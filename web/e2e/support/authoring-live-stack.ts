import { spawn, type ChildProcess } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { get as httpsGet } from "node:https";
import { createServer, type AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

import { expect, type BrowserContext, type Page } from "@playwright/test";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");

export interface AuthoringCredentials {
  login: string;
  password: string;
}

export interface AuthoringStackOptions {
  launcherModule: string;
  manifestName: string;
  transportLogName?: string;
  workspacePrefix: string;
}

export interface AuthoringStack {
  readonly apiUrl: string;
  readonly baseURL: string;
  readonly manifestPath: string;
  readonly transportLogPath: string | null;
  readonly workspace: string;
  readManifest<T>(): Promise<T>;
  restartBackend(): Promise<void>;
  stop(): Promise<void>;
}

async function availableLoopbackPort(): Promise<number> {
  const server = createServer();
  await new Promise<void>((resolveListen, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolveListen());
  });
  const address = server.address() as AddressInfo;
  await new Promise<void>((resolveClose, reject) =>
    server.close((error) => (error ? reject(error) : resolveClose())),
  );
  return address.port;
}

function signal(process: ChildProcess, value: NodeJS.Signals): void {
  if (process.exitCode === null) process.kill(value);
}

async function stopProcess(process: ChildProcess | null, label: string, output: () => string): Promise<void> {
  if (process === null || process.exitCode !== null) return;
  await new Promise<void>((resolveExit, reject) => {
    const forceTimeout = globalThis.setTimeout(() => signal(process, "SIGKILL"), 3_000);
    const failureTimeout = globalThis.setTimeout(() => {
      reject(new Error(`${label} did not stop.\n${output()}`));
    }, 8_000);
    process.once("exit", () => {
      globalThis.clearTimeout(forceTimeout);
      globalThis.clearTimeout(failureTimeout);
      resolveExit();
    });
    signal(process, "SIGTERM");
  });
}

async function waitForApi(process: ChildProcess, apiUrl: string, output: () => string): Promise<void> {
  for (let attempt = 0; attempt < 600; attempt += 1) {
    if (process.exitCode !== null) throw new Error(`Authoring backend exited early.\n${output()}`);
    try {
      const response = await fetch(`${apiUrl}/readyz`, { signal: AbortSignal.timeout(500) });
      if (response.ok) return;
    } catch {
      // The real API may still be migrating or seeding its cassette fixture.
    }
    await delay(100);
  }
  throw new Error(`Authoring backend did not become ready.\n${output()}`);
}

function viteReady(baseURL: string): Promise<boolean> {
  return new Promise((resolveReady) => {
    const request = httpsGet(baseURL, { rejectUnauthorized: false }, (response) => {
      response.resume();
      resolveReady(response.statusCode !== undefined && response.statusCode < 500);
    });
    request.setTimeout(500, () => {
      request.destroy();
      resolveReady(false);
    });
    request.on("error", () => resolveReady(false));
  });
}

export async function startAuthoringStack(options: AuthoringStackOptions): Promise<AuthoringStack> {
  const apiPort = await availableLoopbackPort();
  let vitePort = await availableLoopbackPort();
  while (vitePort === apiPort) vitePort = await availableLoopbackPort();
  const apiUrl = `http://127.0.0.1:${apiPort}`;
  const baseURL = `https://127.0.0.1:${vitePort}`;
  const workspace = await mkdtemp(join(tmpdir(), options.workspacePrefix));
  const manifestPath = join(workspace, options.manifestName);
  const transportLogPath = options.transportLogName ? join(workspace, options.transportLogName) : null;
  let backend: ChildProcess | null = null;
  let backendOutput = "";
  let vite: ChildProcess | null = null;
  let viteOutput = "";

  async function startBackend(): Promise<void> {
    if (backend !== null) throw new Error("Authoring backend is already running.");
    backendOutput = "";
    const python = process.env.GAMEFORGE_PYTHON ?? resolve(repoRoot, ".venv/bin/python");
    const args = [
      "-m",
      options.launcherModule,
      "--workspace",
      workspace,
      "--manifest",
      manifestPath,
      "--port",
      String(apiPort),
      "--web-origin",
      baseURL,
      "--worker",
      "enabled",
    ];
    if (transportLogPath !== null) args.push("--transport-log", transportLogPath);
    const child = spawn(python, args, {
      cwd: repoRoot,
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
      stdio: ["ignore", "pipe", "pipe"],
    });
    child.stdout?.on("data", (chunk: Buffer) => {
      backendOutput += chunk.toString();
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      backendOutput += chunk.toString();
    });
    backend = child;
    try {
      await waitForApi(child, apiUrl, () => backendOutput);
    } catch (error) {
      backend = null;
      signal(child, "SIGTERM");
      throw error;
    }
  }

  async function startVite(): Promise<void> {
    viteOutput = "";
    const child = spawn(
      process.execPath,
      [
        resolve(repoRoot, "web/node_modules/vite/bin/vite.js"),
        "--host",
        "127.0.0.1",
        "--port",
        String(vitePort),
      ],
      {
        cwd: resolve(repoRoot, "web"),
        env: {
          ...process.env,
          GAMEFORGE_WEB_API_TARGET: apiUrl,
          GAMEFORGE_WEB_HMR: "off",
        },
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    child.stdout?.on("data", (chunk: Buffer) => {
      viteOutput += chunk.toString();
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      viteOutput += chunk.toString();
    });
    vite = child;
    for (let attempt = 0; attempt < 300; attempt += 1) {
      if (child.exitCode !== null) throw new Error(`Authoring Vite exited early.\n${viteOutput}`);
      if (await viteReady(baseURL)) return;
      await delay(100);
    }
    throw new Error(`Authoring Vite did not become ready.\n${viteOutput}`);
  }

  try {
    await startBackend();
    await startVite();
  } catch (error) {
    await stopProcess(vite, "Authoring Vite", () => viteOutput).catch(() => undefined);
    await stopProcess(backend, "Authoring backend", () => backendOutput).catch(() => undefined);
    await rm(workspace, { force: true, recursive: true });
    throw error;
  }

  return {
    apiUrl,
    baseURL,
    manifestPath,
    transportLogPath,
    workspace,
    async readManifest<T>() {
      return JSON.parse(await readFile(manifestPath, "utf-8")) as T;
    },
    async restartBackend() {
      const current = backend;
      backend = null;
      await stopProcess(current, "Authoring backend", () => backendOutput);
      await startBackend();
    },
    async stop() {
      const currentVite = vite;
      vite = null;
      const currentBackend = backend;
      backend = null;
      try {
        await stopProcess(currentVite, "Authoring Vite", () => viteOutput);
      } finally {
        try {
          await stopProcess(currentBackend, "Authoring backend", () => backendOutput);
        } finally {
          await rm(workspace, { force: true, recursive: true });
        }
      }
    },
  };
}

export async function guardAuthoringEgress(
  context: BrowserContext,
  baseURL: string,
  unexpected: Set<string>,
): Promise<void> {
  const expectedHttpOrigin = new URL(baseURL).origin;
  const expectedWebSocketOrigin = expectedHttpOrigin.replace(/^http/u, "ws");
  await context.route(
    (url) => ["http:", "https:"].includes(url.protocol) && url.origin !== expectedHttpOrigin,
    async (route) => {
      unexpected.add(new URL(route.request().url()).origin);
      await route.abort("blockedbyclient");
    },
  );
  await context.routeWebSocket(
    (url) => ["ws:", "wss:"].includes(url.protocol) && url.origin !== expectedWebSocketOrigin,
    async (route) => {
      unexpected.add(new URL(route.url()).origin);
      await route.close({ code: 1008, reason: "external egress disabled" });
    },
  );
}

export async function loginAuthoringPage(page: Page, credentials: AuthoringCredentials): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("登录名").fill(credentials.login);
  await page.getByLabel("密码").fill(credentials.password);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page).toHaveURL(/\/specs$/u);
}
