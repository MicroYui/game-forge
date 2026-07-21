import { CircleCheck, Inbox, LoaderCircle, Radio, TriangleAlert } from "lucide-react";

export type ViewState = "empty" | "loading" | "error" | "streaming" | "terminal";

const stateIcons = {
  empty: Inbox,
  loading: LoaderCircle,
  error: TriangleAlert,
  streaming: Radio,
  terminal: CircleCheck,
} as const;

export function StatePanel({
  action,
  description,
  headingLevel = 2,
  state,
  title,
}: {
  action?: React.ReactNode;
  description: string;
  headingLevel?: 1 | 2 | 3;
  state: ViewState;
  title: string;
}) {
  const Icon = stateIcons[state];
  const Heading = headingLevel === 1 ? "h1" : headingLevel === 2 ? "h2" : "h3";
  const role =
    state === "error" ? "alert" : state === "loading" || state === "streaming" ? "status" : undefined;
  return (
    <section className="gf-state-panel" data-state={state} role={role}>
      <Icon aria-hidden="true" size={20} />
      <div>
        <Heading>{title}</Heading>
        <p>{description}</p>
      </div>
      {action}
    </section>
  );
}
