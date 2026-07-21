import { useEffect, useId, useRef, type RefObject } from "react";

import { messages } from "../../i18n/zh-CN";

export function ConfirmDialog({
  confirmLabel,
  description,
  onCancel,
  onConfirm,
  open,
  returnFocusRef,
  title,
}: {
  confirmLabel: string;
  description: string;
  onCancel(): void;
  onConfirm(): void;
  open: boolean;
  returnFocusRef?: RefObject<HTMLElement | null>;
  title: string;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    if (!open) return;
    const returnTarget = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    cancelRef.current?.focus();
    return () => {
      const targetAvailable = returnTarget?.isConnected && !returnTarget.matches(":disabled");
      (targetAvailable ? returnTarget : returnFocusRef?.current)?.focus();
    };
  }, [open, returnFocusRef]);

  if (!open) return null;
  return (
    <div className="gf-dialog-backdrop">
      <section
        aria-describedby={descriptionId}
        aria-labelledby={titleId}
        aria-modal="true"
        className="gf-modal gf-confirm-dialog"
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            onCancel();
            return;
          }
          if (event.key === "Tab" && event.shiftKey && document.activeElement === cancelRef.current) {
            event.preventDefault();
            confirmRef.current?.focus();
          } else if (
            event.key === "Tab" &&
            !event.shiftKey &&
            document.activeElement === confirmRef.current
          ) {
            event.preventDefault();
            cancelRef.current?.focus();
          }
        }}
        role="dialog"
      >
        <h2 id={titleId}>{title}</h2>
        <p id={descriptionId}>{description}</p>
        <div className="gf-cluster gf-confirm-dialog__actions">
          <button onClick={onCancel} ref={cancelRef} type="button">
            {messages.confirm.cancel}
          </button>
          <button data-tone="danger" onClick={onConfirm} ref={confirmRef} type="button">
            {confirmLabel}
          </button>
        </div>
      </section>
    </div>
  );
}
