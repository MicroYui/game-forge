import { useState, type ReactNode } from "react";

/**
 * A disclosure that opens itself while something still needs attention, and then
 * does what the person told it to.
 *
 * Passing React a bare `open` prop that flips to `undefined` looks like it does
 * this, but it does not: React only writes the attribute when the prop changes,
 * so the first toggle a person makes is silently overridden or silently kept
 * forever. Owning the state makes the two rules explicit and orderable — the
 * person's choice wins from the moment they make one.
 */
export function Disclosure({
  children,
  className,
  openWhile = false,
  summary,
}: {
  children: ReactNode;
  className?: string;
  /** Keep it open until the person decides otherwise — e.g. required fields are unset. */
  openWhile?: boolean;
  summary: string;
}) {
  const [chosen, setChosen] = useState<boolean | null>(null);
  return (
    <details
      className={className}
      onToggle={(event) => setChosen(event.currentTarget.open)}
      open={chosen ?? openWhile}
    >
      <summary>{summary}</summary>
      {children}
    </details>
  );
}
