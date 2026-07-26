import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { Disclosure } from "./Disclosure";

function Harness({ initiallyIncomplete = true }: { initiallyIncomplete?: boolean }) {
  const [incomplete, setIncomplete] = useState(initiallyIncomplete);
  return (
    <>
      <button onClick={() => setIncomplete(false)} type="button">
        补齐
      </button>
      <Disclosure openWhile={incomplete} summary="高级设置">
        <label>
          规则格式
          <input />
        </label>
      </Disclosure>
    </>
  );
}

describe("Disclosure", () => {
  it("opens itself while something still needs attention", () => {
    render(<Harness />);

    expect(screen.getByLabelText("规则格式")).toBeVisible();
  });

  it("keeps what the person chose when the form later completes itself", async () => {
    // The bare `open` prop could not do this: React writes the attribute once,
    // so a toggle made before the form completed was stranded — the panel stayed
    // shut and the field it holds was unreachable for the rest of the session.
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByText("高级设置"));
    expect(screen.getByLabelText("规则格式")).not.toBeVisible();

    await user.click(screen.getByRole("button", { name: "补齐" }));
    expect(screen.getByLabelText("规则格式")).not.toBeVisible();

    await user.click(screen.getByText("高级设置"));
    expect(screen.getByLabelText("规则格式")).toBeVisible();
  });

  it("stays shut when nothing needs attention", () => {
    render(<Harness initiallyIncomplete={false} />);

    expect(screen.getByLabelText("规则格式")).not.toBeVisible();
  });
});
