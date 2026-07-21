import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ThemeProvider, useTheme } from "./providers";

function ThemeProbe() {
  const { theme, toggleTheme } = useTheme();
  return (
    <button onClick={toggleTheme} type="button">
      {theme}
    </button>
  );
}

describe("ThemeProvider", () => {
  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
  });

  it("uses the initial system preference and persists an explicit choice", async () => {
    const user = userEvent.setup();
    const matchMedia = vi.fn(
      (query) =>
        ({
          addEventListener: vi.fn(),
          addListener: vi.fn(),
          dispatchEvent: vi.fn(),
          matches: query === "(prefers-color-scheme: dark)",
          media: query,
          onchange: null,
          removeEventListener: vi.fn(),
          removeListener: vi.fn(),
        }) as MediaQueryList,
    );
    Object.defineProperty(window, "matchMedia", { configurable: true, value: matchMedia });

    render(
      <MemoryRouter>
        <ThemeProvider>
          <ThemeProbe />
        </ThemeProvider>
      </MemoryRouter>,
    );

    expect(screen.getByRole("button")).toHaveTextContent("dark");
    await waitFor(() => expect(document.documentElement).toHaveAttribute("data-theme", "dark"));
    await user.click(screen.getByRole("button"));
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
    expect(window.localStorage.getItem("gameforge.theme")).toBe("light");
  });

  it("restores a persisted choice before consulting the system preference", () => {
    window.localStorage.setItem("gameforge.theme", "light");
    const matchMedia = vi.fn(() => ({ matches: true }) as MediaQueryList);
    Object.defineProperty(window, "matchMedia", { configurable: true, value: matchMedia });

    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );

    expect(screen.getByRole("button")).toHaveTextContent("light");
    expect(matchMedia).not.toHaveBeenCalled();
  });
});
