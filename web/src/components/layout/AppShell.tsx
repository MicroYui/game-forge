import {
  Activity,
  ChartNoAxesCombined,
  ChevronRight,
  ClipboardCheck,
  Gamepad2,
  GitCompare,
  LogOut,
  Menu,
  Moon,
  Network,
  ShieldCheck,
  Sparkles,
  Sun,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { useAuth, useTheme, useToast } from "../../app/providers";
import { breadcrumbsFor, navigationRoutes, type NavigationIcon } from "../../app/routes";
import { messages } from "../../i18n/zh-CN";

const navigationIcons = {
  specs: Network,
  generation: Sparkles,
  reviews: ShieldCheck,
  playtest: Gamepad2,
  patches: GitCompare,
  eval: ChartNoAxesCombined,
  observability: Activity,
  approvals: ClipboardCheck,
} satisfies Record<NavigationIcon, typeof Network>;

export function AppShell() {
  const auth = useAuth();
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const { theme, toggleTheme } = useTheme();
  const { pushToast } = useToast();
  const [navigationOpen, setNavigationOpen] = useState(false);
  const navigationToggleRef = useRef<HTMLButtonElement>(null);
  const breadcrumbs = breadcrumbsFor(pathname);
  const principal = auth.status === "authenticated" ? auth.principal : null;

  useEffect(() => {
    setNavigationOpen(false);
    document.getElementById("main-content")?.focus({ preventScroll: true });
  }, [pathname]);

  useEffect(() => {
    if (!navigationOpen) return;
    const closeNavigation = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setNavigationOpen(false);
      navigationToggleRef.current?.focus();
    };
    document.addEventListener("keydown", closeNavigation);
    return () => document.removeEventListener("keydown", closeNavigation);
  }, [navigationOpen]);

  const logout = async () => {
    try {
      await auth.logout();
      navigate("/login", { replace: true });
    } catch {
      pushToast({ message: messages.auth.signOutFailed, tone: "error" });
      await auth.refresh();
    }
  };

  return (
    <div className="gf-shell" data-navigation-open={navigationOpen ? "true" : "false"}>
      <a className="gf-skip-link" href="#main-content">
        {messages.shell.skipToContent}
      </a>
      <aside className="gf-sidebar">
        <div className="gf-brand">
          <NavLink aria-label={messages.app.name} className="gf-brand__link" to="/specs">
            <span aria-hidden="true" className="gf-brand__mark">
              GF
            </span>
            <span>
              <strong>{messages.app.name}</strong>
              <small>{messages.app.descriptor}</small>
            </span>
          </NavLink>
          <button
            aria-controls="primary-navigation"
            aria-expanded={navigationOpen}
            aria-label={navigationOpen ? messages.shell.closeNavigation : messages.shell.openNavigation}
            className="gf-icon-button gf-nav-toggle"
            data-tooltip={navigationOpen ? messages.shell.closeNavigation : messages.shell.openNavigation}
            onClick={() => setNavigationOpen((current) => !current)}
            ref={navigationToggleRef}
            type="button"
          >
            {navigationOpen ? <X aria-hidden="true" size={20} /> : <Menu aria-hidden="true" size={20} />}
          </button>
        </div>
        <nav aria-label={messages.shell.primaryNavigation} className="gf-primary-nav" id="primary-navigation">
          <ul>
            {navigationRoutes.map((route) => {
              const Icon = navigationIcons[route.icon];
              return (
                <li key={route.path}>
                  <NavLink
                    className={({ isActive }) => (isActive ? "gf-nav-link is-active" : "gf-nav-link")}
                    onClick={() => setNavigationOpen(false)}
                    to={route.path}
                  >
                    <Icon aria-hidden="true" size={18} />
                    <span>{route.title}</span>
                  </NavLink>
                </li>
              );
            })}
          </ul>
        </nav>
      </aside>
      <div className="gf-main">
        <header className="gf-topbar">
          <nav aria-label={messages.shell.breadcrumbs} className="gf-breadcrumbs">
            <ol>
              {breadcrumbs.map((item, index) => (
                <li key={`${item.title}:${index}`}>
                  {index > 0 && <ChevronRight aria-hidden="true" size={14} />}
                  {item.path ? (
                    <NavLink to={item.path}>{item.title}</NavLink>
                  ) : (
                    <span aria-current="page">{item.title}</span>
                  )}
                </li>
              ))}
            </ol>
          </nav>
          <div className="gf-identity-bar">
            {principal && (
              <div className="gf-identity" aria-label={messages.shell.currentIdentity} tabIndex={0}>
                <span className="u-chip">{principal.display_name}</span>
                {principal.roles
                  .filter((assignment) => assignment.status === "active" && !assignment.revoked_at)
                  .map((assignment) => (
                    <span className="u-chip" key={assignment.assignment_id}>
                      {messages.roles[assignment.role]}
                    </span>
                  ))}
              </div>
            )}
            <button
              aria-label={theme === "light" ? messages.theme.dark : messages.theme.light}
              className="gf-icon-button"
              data-tooltip={theme === "light" ? messages.theme.dark : messages.theme.light}
              onClick={toggleTheme}
              type="button"
            >
              {theme === "light" ? (
                <Moon aria-hidden="true" size={18} />
              ) : (
                <Sun aria-hidden="true" size={18} />
              )}
            </button>
            <button
              aria-label={messages.auth.signOut}
              className="gf-icon-button"
              data-tooltip={messages.auth.signOut}
              onClick={() => void logout()}
              type="button"
            >
              <LogOut aria-hidden="true" size={18} />
            </button>
          </div>
        </header>
        <main id="main-content" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
