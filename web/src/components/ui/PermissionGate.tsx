import { cloneElement, type ReactElement } from "react";

type DisableableElement = ReactElement<{
  "aria-disabled"?: boolean;
  disabled?: boolean;
}>;

export function PermissionGate({
  allowed,
  children,
  mode = "hide",
}: {
  allowed: boolean;
  children: DisableableElement;
  mode?: "hide" | "disable";
}) {
  if (allowed) return children;
  if (mode === "hide") return null;
  return cloneElement(children, { "aria-disabled": true, disabled: true });
}
