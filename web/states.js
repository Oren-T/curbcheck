/**
 * The states that are not a result: errors, degraded data, and toasts.
 *
 * DESIGN_DIRECTION §5: nothing safety-relevant is ever a toast, because a toast
 * disappears. Errors and degraded-data notices are persistent blocks in the
 * rail; the toast is reserved for transient confirmations ("Pin moved").
 * UX_AUDIT P1-10 also asks that "no answer" and "no parking" stop looking
 * alike, which is why an error is a bordered block with a retry, not a line of
 * red text where the status line goes.
 */

import { clear, el, replaceChildren } from "./dom.js";

const TOAST_MS = 4000;

/**
 * Put one persistent notice in the rail, replacing any previous one.
 *
 * @param {HTMLElement} container
 * @param {{kind: "error"|"warn", title?: string, text: string,
 *          actionLabel?: string, onAction?: Function}|null} notice
 */
export function renderNotice(container, notice) {
  if (!notice) {
    clear(container);
    return;
  }
  const action = notice.actionLabel
    ? el("button", { className: "ghost", text: notice.actionLabel, attrs: { type: "button" } })
    : null;
  if (action && notice.onAction) {
    action.addEventListener("click", () => notice.onAction());
  }
  replaceChildren(container, [
    el("div", { className: `notice-block notice-${notice.kind}` }, [
      notice.title ? el("h3", { text: notice.title }) : null,
      el("p", { text: notice.text }),
      action,
    ]),
  ]);
}

/** A transient confirmation. Never a verdict, a caveat, or an error. */
export function showToast(container, message) {
  const toast = el("div", { className: "toast" }, [el("span", { text: message })]);
  container.append(toast);
  setTimeout(() => toast.remove(), TOAST_MS);
}
