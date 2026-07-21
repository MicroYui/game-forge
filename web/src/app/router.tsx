import { useState, type FormEvent } from "react";
import { Navigate, Outlet, useLocation, useNavigate, useParams } from "react-router-dom";

import { ApiProblemError, type SafeProblem } from "../api/problem";
import { AppShell } from "../components/layout";
import { ProblemPanel, StatePanel } from "../components/ui";
import { RunDetailPage } from "../features/runs";
import { messages } from "../i18n/zh-CN";
import { useAuth } from "./providers";

type ReturnLocationState = { returnTo?: string };

function safeReturnPath(value: unknown): string {
  return typeof value === "string" && value.startsWith("/") && !value.startsWith("//") && value !== "/login"
    ? value
    : "/specs";
}

export function RequireAuth() {
  const auth = useAuth();
  const location = useLocation();
  if (auth.status === "loading") {
    return (
      <main className="gf-auth-page">
        <StatePanel
          description={messages.auth.hydrationProgress}
          headingLevel={1}
          state="loading"
          title={messages.states.loading}
        />
      </main>
    );
  }
  if (auth.status === "error") {
    return (
      <main className="gf-auth-page">
        <StatePanel
          action={
            <button onClick={() => void auth.refresh()} type="button">
              {messages.auth.retryHydration}
            </button>
          }
          description={messages.auth.hydrationError}
          headingLevel={1}
          state="error"
          title={messages.states.error}
        />
      </main>
    );
  }
  if (auth.status === "anonymous") {
    return <Navigate replace state={{ returnTo: `${location.pathname}${location.search}` }} to="/login" />;
  }
  return <Outlet />;
}

export function LoginPage() {
  const auth = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [loginName, setLoginName] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [problem, setProblem] = useState<SafeProblem | null>(null);
  const [unexpectedError, setUnexpectedError] = useState(false);
  const returnTo = safeReturnPath((location.state as ReturnLocationState | null)?.returnTo);

  if (auth.status === "authenticated") return <Navigate replace to={returnTo} />;
  if (auth.status === "loading") {
    return (
      <main className="gf-auth-page">
        <StatePanel
          description={messages.auth.hydrationProgress}
          headingLevel={1}
          state="loading"
          title={messages.states.loading}
        />
      </main>
    );
  }
  if (auth.status === "error") {
    return (
      <main className="gf-auth-page">
        <StatePanel
          action={
            <button onClick={() => void auth.refresh()} type="button">
              {messages.auth.retryHydration}
            </button>
          }
          description={messages.auth.hydrationError}
          headingLevel={1}
          state="error"
          title={messages.states.error}
        />
      </main>
    );
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setProblem(null);
    setUnexpectedError(false);
    try {
      await auth.login({ login_name: loginName, password, schema_version: "password-auth@1" });
      setPassword("");
      navigate(returnTo, { replace: true });
    } catch (error) {
      setPassword("");
      if (error instanceof ApiProblemError) setProblem(error.problem);
      else setUnexpectedError(true);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="gf-auth-page">
      <section className="gf-auth-card">
        <header className="gf-page-header">
          <p className="u-small">{messages.app.descriptor}</p>
          <h1>{messages.auth.signIn}</h1>
          <p>{messages.auth.loginDescription}</p>
        </header>
        {problem && <ProblemPanel problem={problem} />}
        {unexpectedError && <p role="alert">{messages.auth.signInFailed}</p>}
        <form className="gf-form" onSubmit={(event) => void submit(event)}>
          <label>
            <span>{messages.auth.loginName}</span>
            <input
              autoComplete="username"
              autoFocus
              maxLength={256}
              name="login_name"
              onChange={(event) => setLoginName(event.target.value)}
              required
              value={loginName}
            />
          </label>
          <label>
            <span>{messages.auth.password}</span>
            <input
              autoComplete="current-password"
              maxLength={4096}
              name="password"
              onChange={(event) => setPassword(event.target.value)}
              required
              type="password"
              value={password}
            />
          </label>
          <button disabled={submitting} type="submit">
            {submitting ? messages.auth.signingIn : messages.auth.signInAction}
          </button>
        </form>
      </section>
    </main>
  );
}

export function RunDetailRoute() {
  const { runId } = useParams<{ runId: string }>();
  if (!runId) return <Navigate replace to="/observability" />;
  return <RunDetailPage runId={runId} />;
}

export function NotFoundPage() {
  return (
    <div className="gf-page">
      <StatePanel
        description={messages.placeholders.notFoundDescription}
        headingLevel={1}
        state="empty"
        title={messages.details.notFound}
      />
    </div>
  );
}

export { AppShell };
