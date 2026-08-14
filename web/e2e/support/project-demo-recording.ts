import { mkdir } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, type Locator, type Page } from "@playwright/test";

import {
  PROJECT_DEMO_PROVENANCE_LABEL,
  PROJECT_DEMO_README_FRAMES,
  PROJECT_DEMO_SCENES,
  type ProjectDemoScene,
} from "../../scripts/project-demo-storyboard";

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");

export interface ProjectDemoOutput {
  coverPath: string;
  framesDirectory: string;
  rawVideoDirectory: string;
  videoPath: string;
}

export async function prepareProjectDemoOutput(): Promise<ProjectDemoOutput> {
  const outputDirectory =
    process.env.GAMEFORGE_DEMO_OUTPUT_DIR ?? resolve(webRoot, "test-results/demo-project-workflow");
  const output = {
    coverPath: join(outputDirectory, "hero-project-workflow-zh.png"),
    framesDirectory: join(outputDirectory, "readme-frames-project-workflow-zh"),
    rawVideoDirectory: join(outputDirectory, "raw-project-workflow-zh"),
    videoPath: join(outputDirectory, "gameforge-project-workflow-zh.webm"),
  };
  await Promise.all([
    mkdir(output.framesDirectory, { recursive: true }),
    mkdir(output.rawVideoDirectory, { recursive: true }),
  ]);
  return output;
}

function requiredScene(key: string): ProjectDemoScene {
  const scene = PROJECT_DEMO_SCENES.find((candidate) => candidate.key === key);
  if (scene === undefined) throw new Error(`Unknown project demo scene: ${key}`);
  return scene;
}

async function scrollTargetIntoView(page: Page, target: Locator): Promise<void> {
  await expect(target).toBeVisible({ timeout: 30_000 });
  await target.evaluate((element) => {
    element.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
  });
  await page.waitForTimeout(550);
}

async function renderOverlay(page: Page, scene: ProjectDemoScene): Promise<void> {
  const index = PROJECT_DEMO_SCENES.findIndex((candidate) => candidate.key === scene.key);
  await page.evaluate(
    ({ index, provenance, scene, total }) => {
      document.getElementById("gf-project-demo-overlay")?.remove();
      document.getElementById("gf-project-demo-style")?.remove();

      const style = document.createElement("style");
      style.id = "gf-project-demo-style";
      style.textContent = `
        #gf-project-demo-overlay {
          color: #f5f7f4;
          font-family: "GameForge Editorial Serif", "Source Han Serif SC", "Songti SC", serif;
          inset: 0;
          pointer-events: none;
          position: fixed;
          z-index: 2147483000;
        }
        #gf-project-demo-overlay * { box-sizing: border-box; }
        #gf-project-demo-overlay .gf-project-demo__scrim {
          background: linear-gradient(180deg, transparent 0%, rgba(8, 11, 9, 0.3) 22%, rgba(8, 11, 9, 0.96) 100%);
          bottom: 0;
          height: 235px;
          left: 0;
          position: absolute;
          right: 0;
        }
        #gf-project-demo-overlay .gf-project-demo__caption {
          bottom: 32px;
          left: 38px;
          max-width: 830px;
          position: absolute;
          z-index: 2;
        }
        #gf-project-demo-overlay .gf-project-demo__kicker {
          color: #80d2c9;
          font-family: "SF Mono", ui-monospace, monospace;
          font-size: 11px;
          letter-spacing: 0.14em;
          margin: 0 0 8px;
          text-transform: uppercase;
        }
        #gf-project-demo-overlay .gf-project-demo__title {
          color: #f8faf7;
          font-size: 30px;
          font-weight: 600;
          letter-spacing: -0.025em;
          line-height: 1.08;
          margin: 0;
          text-wrap: balance;
        }
        #gf-project-demo-overlay .gf-project-demo__body {
          color: #cbd2cc;
          font-family: system-ui, sans-serif;
          font-size: 14px;
          line-height: 1.5;
          margin: 9px 0 0;
          max-width: 750px;
        }
        #gf-project-demo-overlay .gf-project-demo__provenance {
          align-items: center;
          backdrop-filter: blur(12px);
          background: rgba(14, 18, 15, 0.88);
          border: 1px solid rgba(128, 210, 201, 0.38);
          border-radius: 999px;
          bottom: 18px;
          color: #b1e6df;
          display: flex;
          font-family: "SF Mono", ui-monospace, monospace;
          font-size: 9px;
          gap: 8px;
          letter-spacing: 0.05em;
          padding: 8px 12px;
          position: absolute;
          right: 22px;
          z-index: 3;
        }
        #gf-project-demo-overlay .gf-project-demo__dot {
          background: #80d2c9;
          border-radius: 50%;
          box-shadow: 0 0 0 4px rgba(128, 210, 201, 0.13);
          height: 6px;
          width: 6px;
        }
        #gf-project-demo-overlay .gf-project-demo__progress {
          background: rgba(255, 255, 255, 0.12);
          bottom: 0;
          height: 2px;
          left: 0;
          position: absolute;
          right: 0;
        }
        #gf-project-demo-overlay .gf-project-demo__progress > span {
          background: linear-gradient(90deg, #3f8e86, #80d2c9);
          display: block;
          height: 100%;
        }
        #gf-project-demo-overlay.gf-project-demo--hero {
          background:
            radial-gradient(circle at 79% 20%, rgba(128, 210, 201, 0.2), transparent 30%),
            radial-gradient(circle at 20% 84%, rgba(149, 171, 238, 0.12), transparent 34%),
            linear-gradient(135deg, #101411 0%, #19201b 55%, #0b0e0c 100%);
        }
        #gf-project-demo-overlay.gf-project-demo--hero::after {
          border: 1px solid rgba(128, 210, 201, 0.15);
          content: "";
          inset: 32px;
          position: absolute;
        }
        #gf-project-demo-overlay.gf-project-demo--hero .gf-project-demo__caption {
          bottom: auto;
          left: 118px;
          max-width: 920px;
          top: 50%;
          transform: translateY(-52%);
        }
        #gf-project-demo-overlay.gf-project-demo--hero .gf-project-demo__title {
          font-size: 66px;
          letter-spacing: -0.04em;
          line-height: 1;
        }
        #gf-project-demo-overlay.gf-project-demo--hero .gf-project-demo__body {
          color: #b7c0b8;
          font-size: 18px;
          margin-top: 20px;
        }
        #gf-project-demo-overlay.gf-project-demo--hero .gf-project-demo__provenance {
          bottom: auto;
          right: 22px;
          top: 18px;
        }
      `;
      document.head.append(style);

      const overlay = document.createElement("div");
      overlay.id = "gf-project-demo-overlay";
      if (scene.variant === "hero") overlay.classList.add("gf-project-demo--hero");

      if (scene.variant !== "hero") {
        const scrim = document.createElement("div");
        scrim.className = "gf-project-demo__scrim";
        overlay.append(scrim);
      }

      const caption = document.createElement("section");
      caption.className = "gf-project-demo__caption";
      const kicker = document.createElement("p");
      kicker.className = "gf-project-demo__kicker";
      kicker.textContent = scene.kicker;
      const title = document.createElement("h2");
      title.className = "gf-project-demo__title";
      title.textContent = scene.title;
      const body = document.createElement("p");
      body.className = "gf-project-demo__body";
      body.textContent = scene.body;
      caption.append(kicker, title, body);
      overlay.append(caption);

      const provenanceBadge = document.createElement("div");
      provenanceBadge.className = "gf-project-demo__provenance";
      const dot = document.createElement("span");
      dot.className = "gf-project-demo__dot";
      const provenanceCopy = document.createElement("span");
      provenanceCopy.textContent = provenance;
      provenanceBadge.append(dot, provenanceCopy);
      overlay.append(provenanceBadge);

      const progress = document.createElement("div");
      progress.className = "gf-project-demo__progress";
      const progressValue = document.createElement("span");
      progressValue.style.width = `${((index + 1) / total) * 100}%`;
      progress.append(progressValue);
      overlay.append(progress);

      document.body.append(overlay);
      overlay.animate([{ opacity: 0 }, { opacity: 1 }], {
        duration: 320,
        easing: "cubic-bezier(.2,.8,.2,1)",
        fill: "forwards",
      });
    },
    {
      index,
      provenance: PROJECT_DEMO_PROVENANCE_LABEL,
      scene,
      total: PROJECT_DEMO_SCENES.length,
    },
  );
}

export async function showProjectDemoScene(
  page: Page,
  output: ProjectDemoOutput,
  key: string,
  options: { cover?: boolean; target?: Locator; targetFinishAt?: number } = {},
): Promise<void> {
  const scene = requiredScene(key);
  if (options.target) await scrollTargetIntoView(page, options.target);
  await page.evaluate(async () => document.fonts.ready);
  await renderOverlay(page, scene);

  const frame = PROJECT_DEMO_README_FRAMES.find((candidate) => candidate.sceneKey === key);
  if (frame !== undefined) {
    await page.screenshot({
      animations: "disabled",
      path: join(output.framesDirectory, frame.filename),
    });
  }
  if (options.cover) {
    await page.screenshot({ animations: "disabled", path: output.coverPath });
  }

  const targetHold = options.targetFinishAt === undefined ? 0 : options.targetFinishAt - Date.now();
  await page.waitForTimeout(Math.max(scene.holdMs, targetHold));
  await page.evaluate(() => {
    document.getElementById("gf-project-demo-overlay")?.remove();
    document.getElementById("gf-project-demo-style")?.remove();
  });
}
