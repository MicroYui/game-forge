import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { X } from "lucide-react";

import type { GameForgeApi } from "../api/client";
import { createMutationIntent } from "../api/csrf";
import { ApiProblemError } from "../api/problem";
import { gameForgeApi, subscribeSessionBoundary } from "../api/runtime";
import { messages } from "../i18n/zh-CN";
import type { AuthenticatedPrincipal, PasswordAuthRequest } from "./auth-types";

export type AuthApi = Pick<GameForgeApi, "login" | "logout" | "me">;
export type SessionBoundarySubscriber = (listener: () => void) => () => void;

type AuthState =
  | { status: "loading"; principal: null; error: null }
  | { status: "anonymous"; principal: null; error: null }
  | { status: "authenticated"; principal: AuthenticatedPrincipal; error: null }
  | { status: "error"; principal: null; error: Error };

type AuthContextValue = AuthState & {
  login(request: PasswordAuthRequest): Promise<void>;
  logout(): Promise<void>;
  refresh(): Promise<void>;
  expireSession(): void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

function normalizedError(error: unknown): Error {
  return error instanceof Error ? error : new Error(messages.auth.hydrationError);
}

function isUnauthenticated(error: unknown): boolean {
  return error instanceof ApiProblemError && error.problem.status === 401;
}

export function AuthProvider({
  api = gameForgeApi,
  children,
  subscribeToSessionBoundary = subscribeSessionBoundary,
}: PropsWithChildren<{ api?: AuthApi; subscribeToSessionBoundary?: SessionBoundarySubscriber }>) {
  const [state, setState] = useState<AuthState>({ error: null, principal: null, status: "loading" });
  const expireSession = useCallback(() => {
    setState({ error: null, principal: null, status: "anonymous" });
  }, []);

  const refresh = useCallback(async () => {
    setState({ error: null, principal: null, status: "loading" });
    try {
      const principal = await api.me();
      setState({ error: null, principal, status: "authenticated" });
    } catch (error) {
      setState(
        isUnauthenticated(error)
          ? { error: null, principal: null, status: "anonymous" }
          : { error: normalizedError(error), principal: null, status: "error" },
      );
    }
  }, [api]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => subscribeToSessionBoundary(expireSession), [expireSession, subscribeToSessionBoundary]);

  const value = useMemo<AuthContextValue>(
    () => ({
      ...state,
      async login(request) {
        await api.login(request);
        const principal = await api.me();
        setState({ error: null, principal, status: "authenticated" });
      },
      async logout() {
        await api.logout(createMutationIntent());
        setState({ error: null, principal: null, status: "anonymous" });
      },
      refresh,
      expireSession,
    }),
    [api, expireSession, refresh, state],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (value === null) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}

export type Theme = "light" | "dark";

type ThemeContextValue = {
  theme: Theme;
  setTheme(theme: Theme): void;
  toggleTheme(): void;
};

const THEME_STORAGE_KEY = "gameforge.theme";
const ThemeContext = createContext<ThemeContextValue | null>(null);

function initialTheme(): Theme {
  const bootstrapped = document.documentElement.dataset.theme;
  if (bootstrapped === "light" || bootstrapped === "dark") return bootstrapped;
  const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
  if (stored === "light" || stored === "dark") return stored;
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export function ThemeProvider({ children }: PropsWithChildren) {
  const [theme, setTheme] = useState<Theme>(initialTheme);

  useLayoutEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  }, [theme]);

  const value = useMemo<ThemeContextValue>(
    () => ({
      setTheme,
      theme,
      toggleTheme() {
        setTheme((current) => (current === "light" ? "dark" : "light"));
      },
    }),
    [theme],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext);
  if (value === null) throw new Error("useTheme must be used inside ThemeProvider");
  return value;
}

export type ToastTone = "info" | "success" | "error";

export type ToastInput = {
  message: string;
  title?: string;
  tone: ToastTone;
};

type ToastEntry = ToastInput & { id: number };

type ToastContextValue = {
  pushToast(input: ToastInput): number;
  dismissToast(id: number): void;
};

const ToastContext = createContext<ToastContextValue | null>(null);

export function ToastProvider({ children }: PropsWithChildren) {
  const [toasts, setToasts] = useState<ToastEntry[]>([]);
  const nextId = useRef(1);
  const value = useMemo<ToastContextValue>(
    () => ({
      dismissToast(id) {
        setToasts((current) => current.filter((toast) => toast.id !== id));
      },
      pushToast(input) {
        const id = nextId.current++;
        setToasts((current) => [...current, { ...input, id }]);
        return id;
      },
    }),
    [],
  );

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div aria-label={messages.toast.viewport} className="gf-toast-viewport" role="region">
        {toasts.map((toast) => (
          <section
            className="gf-toast"
            data-tone={toast.tone}
            key={toast.id}
            role={toast.tone === "error" ? "alert" : "status"}
          >
            <div>
              <strong>{toast.title ?? messages.toast[toast.tone]}</strong>
              <p>{toast.message}</p>
            </div>
            <button
              aria-label={messages.toast.dismiss}
              className="gf-icon-button"
              data-tooltip={messages.toast.dismiss}
              onClick={() => value.dismissToast(toast.id)}
              type="button"
            >
              <X aria-hidden="true" size={18} />
            </button>
          </section>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const value = useContext(ToastContext);
  if (value === null) throw new Error("useToast must be used inside ToastProvider");
  return value;
}

export function AppProviders({ children }: PropsWithChildren) {
  return (
    <ThemeProvider>
      <ToastProvider>
        <AuthProvider>{children}</AuthProvider>
      </ToastProvider>
    </ThemeProvider>
  );
}
