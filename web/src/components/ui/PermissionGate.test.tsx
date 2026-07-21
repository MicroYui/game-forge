import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PermissionGate } from "./PermissionGate";

describe("PermissionGate", () => {
  it("renders an explicitly server-allowed action", () => {
    render(
      <PermissionGate allowed>
        <button type="button">批准</button>
      </PermissionGate>,
    );

    expect(screen.getByRole("button", { name: "批准" })).toBeEnabled();
  });

  it("hides or disables only from the explicit boolean", () => {
    const { rerender } = render(
      <PermissionGate allowed={false}>
        <button type="button">发布</button>
      </PermissionGate>,
    );
    expect(screen.queryByRole("button", { name: "发布" })).not.toBeInTheDocument();

    rerender(
      <PermissionGate allowed={false} mode="disable">
        <button type="button">发布</button>
      </PermissionGate>,
    );
    expect(screen.getByRole("button", { name: "发布" })).toBeDisabled();
  });
});
