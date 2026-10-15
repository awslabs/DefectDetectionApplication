/**
 * What the Triple_HMI shows of the API target (`api/base.ts`).
 *
 * The login form names the LocalServer `host:port` the credentials are posted
 * to, and a `?api=` value the host policy ignored is reported in a visible,
 * non-blocking, dismissible notice naming the host. The page URL is set the
 * way a link would set it, so the real resolution runs.
 *
 * @vitest-environment jsdom
 */

import { afterEach, describe, expect, it } from "vitest";

import { resetApiBase, setApiBase } from "../api/base";
import { initialTripleState } from "./machine";
import { TRIPLE_MESSAGES, createTripleRenderer } from "./render";

const NAME = "blue-plate-detection-guided-inspection";

function mount(): { root: HTMLElement; render: (screen: "login" | "app") => void } {
  document.body.replaceChildren();
  const root = document.createElement("div");
  document.body.append(root);
  const renderer = createTripleRenderer(
    root,
    {
      onLoginSubmit: () => undefined,
      onHistorySelect: () => undefined,
      onReturnToLive: () => undefined,
    },
    { loadImage: () => undefined },
  );
  return { root, render: (screen) => renderer.render(initialTripleState(screen, NAME)) };
}

function one(scope: ParentNode, selector: string): HTMLElement {
  const found = scope.querySelector<HTMLElement>(selector);
  if (found === null) throw new Error(`no element matching ${selector}`);
  return found;
}

function visible(node: HTMLElement): boolean {
  return !node.classList.contains("hidden");
}

/** Opens the kiosk page with `search` as its query string, as a link would. */
function openWithQuery(search: string): void {
  history.replaceState(null, "", `/hmi/triple.html${search}`);
  resetApiBase();
}

/** This jsdom page's own host:port (the same-origin fallback). */
function pageAddress(): string {
  return `${location.hostname}:${location.port || "80"}`;
}

afterEach(() => {
  history.replaceState(null, "", "/");
  resetApiBase();
});

describe("login form target (where the credentials are posted)", () => {
  it.each([
    ["http://192.168.8.224:5000", "192.168.8.224:5000"],
    ["https://station-01.local", "station-01.local:443"],
    ["http://station-01", "station-01:80"],
    ["http://[fe80::1]:5000", "[fe80::1]:5000"],
  ])("names %s as %s", (base, address) => {
    setApiBase(base);
    const { root, render } = mount();
    render("login");

    const target = one(root, ".login-target");
    expect(visible(target)).toBe(true);
    expect(target.textContent).toBe(`${TRIPLE_MESSAGES.loginTarget} ${address}`);
    expect(one(root, "form.login-box").contains(target)).toBe(true);
  });

  it("names this page's own host:port for a same-origin bundle", () => {
    openWithQuery("");
    const { root, render } = mount();
    render("login");
    expect(one(root, ".login-target-address").textContent).toBe(pageAddress());
  });

  it("names the normalized host a ?api= link points the form at", () => {
    openWithQuery("?api=http://3232235786:5000"); // decimal 192.168.1.10
    const { root, render } = mount();
    render("login");
    expect(one(root, ".login-target-address").textContent).toBe("192.168.1.10:5000");
  });
});

describe("ignored ?api= notice", () => {
  it("names the rejected host without blocking the login form", () => {
    openWithQuery("?api=https://other.example:5000");
    const { root, render } = mount();
    render("login");

    const notice = one(root, ".api-notice");
    expect(visible(notice)).toBe(true);
    expect(notice.getAttribute("role")).toBe("status");
    expect(one(notice, ".api-notice-text").textContent).toBe(
      'Ignored ?api= host "other.example": it is not this page\'s host, a private-network ' +
        `address or an allowed host. Using ${pageAddress()} instead.`,
    );
    // The form stays usable and names the fallback, not the ignored host.
    expect(visible(one(root, ".login-screen"))).toBe(true);
    expect(one(root, ".login-target-address").textContent).toBe(pageAddress());
  });

  it("shows on the kiosk view too, and Dismiss hides it for good", () => {
    openWithQuery("?api=http://op@192.168.8.224:5000");
    const { root, render } = mount();
    render("app");

    const notice = one(root, ".api-notice");
    expect(visible(notice)).toBe(true);
    expect(notice.textContent).toContain('"192.168.8.224"');
    expect(notice.textContent).toContain("user name or password");

    const dismiss = one(notice, "button") as HTMLButtonElement;
    expect(dismiss.type).toBe("button");
    expect(dismiss.textContent).toBe(TRIPLE_MESSAGES.dismissNotice);
    dismiss.click();
    expect(visible(notice)).toBe(false);
    render("app");
    expect(visible(notice)).toBe(false);
  });

  it("does not rewrite its text on every render, so it is announced once", () => {
    openWithQuery("?api=https://other.example");
    const { root, render } = mount();
    render("app");
    const text = one(root, ".api-notice-text").firstChild;
    render("app");
    render("login");
    expect(one(root, ".api-notice-text").firstChild).toBe(text);
  });

  it("stays hidden when ?api= is accepted or absent", () => {
    for (const search of ["?api=http://192.168.8.224:5000", "?api=5000", ""]) {
      openWithQuery(search);
      const { root, render } = mount();
      render("login");
      expect(visible(one(root, ".api-notice")), search).toBe(false);
    }
  });
});
