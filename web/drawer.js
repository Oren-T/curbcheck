/**
 * The phone bottom drawer: the rail at three detents, draggable by its handle.
 *
 * UX_AUDIT P0-4 is the reason this exists. On a phone the old page put the map
 * below 590 result cards in one 87,609 px document, so tapping a card opened a
 * sheet 86,000 px off screen and, to the user, nothing happened. The drawer is
 * a fixed-height layer with its own scroll container; the document behind it
 * never grows.
 *
 * Above 640 px the rail is a fixed sidebar and every function here is a no-op.
 */

const PHONE_QUERY = "(max-width: 640px)";
const DETENTS = ["peek", "half", "full"];

/**
 * @param {{rail: HTMLElement, handle: HTMLElement, summary: HTMLElement,
 *          onDetent?: Function}} options
 */
export function createDrawer({ rail, handle, summary, onDetent = () => {} }) {
  let detent = "peek";
  let drag = null;

  const phone = () => window.matchMedia(PHONE_QUERY).matches;

  function apply(next) {
    detent = next;
    rail.dataset.detent = next;
    rail.style.height = "";
    handle.setAttribute("aria-expanded", String(next !== "peek"));
    onDetent(next);
  }

  function detentHeights() {
    const available = rail.parentElement.clientHeight;
    return {
      peek: parseFloat(window.getComputedStyle(rail).getPropertyValue("--drawer-peek")) || 132,
      half: available * 0.58,
      full: available - 12,
    };
  }

  function nearest(height) {
    const heights = detentHeights();
    return DETENTS.reduce((best, name) =>
      Math.abs(heights[name] - height) < Math.abs(heights[best] - height) ? name : best,
    );
  }

  handle.addEventListener("click", () => {
    if (!phone()) {
      return;
    }
    // A tap is the keyboard- and screen-reader-reachable way to move between
    // detents; the drag below is the same control with a pointer.
    apply(detent === "peek" ? "half" : detent === "half" ? "full" : "peek");
  });

  handle.addEventListener("pointerdown", (event) => {
    if (!phone()) {
      return;
    }
    drag = { y: event.clientY, height: rail.getBoundingClientRect().height, moved: false };
    handle.setPointerCapture(event.pointerId);
  });

  handle.addEventListener("pointermove", (event) => {
    if (!drag) {
      return;
    }
    const height = drag.height - (event.clientY - drag.y);
    if (Math.abs(event.clientY - drag.y) > 4) {
      drag.moved = true;
    }
    rail.style.height = `${Math.max(64, height)}px`;
  });

  function endDrag(event) {
    if (!drag) {
      return;
    }
    const height = rail.getBoundingClientRect().height;
    const moved = drag.moved;
    drag = null;
    handle.releasePointerCapture(event.pointerId);
    if (moved) {
      apply(nearest(height));
    } else {
      rail.style.height = "";
    }
  }

  handle.addEventListener("pointerup", endDrag);
  handle.addEventListener("pointercancel", endDrag);

  return {
    isPhone: phone,
    detent: () => detent,
    open(next = "half") {
      if (phone()) {
        apply(next);
      }
    },
    collapse() {
      if (phone()) {
        apply("peek");
      }
    },
    setSummary(text) {
      summary.textContent = text;
    },
  };
}
