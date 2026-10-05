import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { Label, toggleNode } from "./Label";
import { Performance, isWeak } from "./Performance";
import type { NodeMetric, QueueItem, TaxNode } from "./api";

const node = (id: number, parent_id: number | null, path: string): TaxNode => ({
  id,
  parent_id,
  path,
  name: path.split("/").at(-1) ?? "",
  title: path.split("/").at(-1) ?? "",
  depth: path.split("/").length,
  n_labels: 0,
  n_review: 0,
});
const NODES = [
  node(1, null, "hotel"),
  node(2, 1, "hotel/rooms"),
  node(3, 2, "hotel/rooms/comfort"),
  node(4, 2, "hotel/rooms/cleanliness"),
  node(5, null, "laptop"),
];
const item = (id: number, text: string, over: Partial<QueueItem> = {}): QueueItem => ({
  id,
  text,
  lang: "en",
  source: "feed",
  created_at: null,
  is_late: false,
  split: "pool",
  confidence: 0.62,
  uncertainty: 1.1,
  scored_by_model: 7,
  current_node_ids: [],
  review_reason: null,
  suggestions: [
    { node_id: 1, prob: 0.9, selected: true },
    { node_id: 2, prob: 0.7, selected: true },
    { node_id: 3, prob: 0.3, selected: false },
  ],
  ...over,
});

type Reply = { status?: number; body: unknown };
type Handler = (url: string, init?: RequestInit) => Reply | undefined;
function mockApi(handler: Handler) {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      const r = handler(url, init) ?? {
        status: 404,
        body: { error: { code: "not_found", message: `no mock for ${url}`, request_id: "r" } },
      };
      return new Response(JSON.stringify(r.body), { status: r.status ?? 200 });
    }),
  );
  return calls;
}
const withClient = (ui: ReactElement) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      {ui}
    </QueryClientProvider>,
  );
const taxonomy: Reply = { body: { version: 3, nodes: NODES } };
const page = (items: QueueItem[], extra = {}): Reply => ({
  body: { items, next_cursor: null, active_model: 7, remaining: items.length, ...extra },
});
const fail = (status: number, code: string, message: string, request_id = "r"): Reply => ({
  status,
  body: { error: { code, message, request_id } },
});

describe("toggleNode keeps the selection parent-consistent", () => {
  const byId = new Map(NODES.map((n) => [n.id, n]));
  it("adds ancestors when a child is selected", () => {
    expect([...toggleNode(new Set(), 3, byId)].sort()).toEqual([1, 2, 3]);
  });
  it("removes descendants when a parent is removed, and nothing else", () => {
    expect([...toggleNode(new Set([1, 2, 3, 4, 5]), 2, byId)].sort()).toEqual([1, 5]);
  });
});

describe("Label", () => {
  it("shows loading, then the item with the model picks pre-selected", async () => {
    mockApi((url) =>
      url.includes("/taxonomy") ? taxonomy : url.includes("/queue") ? page([item(10, "The bed was awful")]) : undefined,
    );
    withClient(<Label />);
    expect(screen.getByText(/Loading the labelling queue/)).toBeInTheDocument();
    const heading = await screen.findByRole("heading", { name: "The bed was awful" });
    await waitFor(() => expect(heading).toHaveFocus()); // focus moves in an effect, one tick after render
    const boxes = screen.getAllByRole("checkbox");
    expect(boxes.map((b) => (b as HTMLInputElement).checked)).toEqual([true, true, false]);
    expect(screen.getByText(/Model confidence 62%/)).toBeInTheDocument();
  });

  it("keyboard: digit toggles a suggestion, Enter saves the closed set and advances", async () => {
    const six = [10, 11, 12, 13, 14, 15].map((id, i) => item(id, ["first", "second", "c", "d", "e", "f"][i] ?? ""));
    const calls = mockApi((url, init) =>
      url.includes("/taxonomy")
        ? taxonomy
        : url.includes("/queue")
          ? page(six)
          : init?.method === "PUT"
            ? { body: { feedback_id: 10, node_ids: [1, 2, 3] } }
            : undefined,
    );
    const user = userEvent.setup();
    withClient(<Label />);
    await screen.findByRole("heading", { name: "first" });
    await user.keyboard("3");
    expect((screen.getAllByRole("checkbox")[2] as HTMLInputElement).checked).toBe(true);
    await user.keyboard("{Enter}");
    const second = await screen.findByRole("heading", { name: "second" });
    await waitFor(() => expect(second).toHaveFocus());
    const put = calls.find((c) => c.init?.method === "PUT");
    expect(put?.url).toBe("/api/v1/items/10/annotation");
    expect(JSON.parse(String(put?.init?.body)).node_ids.sort()).toEqual([1, 2, 3]);
    await waitFor(() => expect(screen.getByTestId("progress")).toHaveTextContent("1 labelled this session"));
  });

  it("puts the item back and says why when saving fails", async () => {
    mockApi((url, init) =>
      url.includes("/taxonomy")
        ? taxonomy
        : url.includes("/queue")
          ? page([item(10, "first"), item(11, "second")])
          : init?.method === "PUT"
            ? fail(503, "database_unavailable", "The database is not reachable right now.", "abcdef123456")
            : undefined,
    );
    const user = userEvent.setup();
    withClient(<Label />);
    await screen.findByRole("heading", { name: "first" });
    await user.click(screen.getByRole("button", { name: /Save & next/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The database is not reachable right now.");
    expect(screen.getByRole("alert")).toHaveTextContent("abcdef12");
    expect(screen.getByRole("heading", { name: "first" })).toBeInTheDocument();
  });

  it("explains an empty queue, a missing model, and a failed load differently", async () => {
    mockApi((url) => (url.includes("/taxonomy") ? taxonomy : page([])));
    const a = withClient(<Label />);
    expect(await screen.findByText(/Queue empty/)).toBeInTheDocument();
    a.unmount();
    mockApi((url) => (url.includes("/taxonomy") ? taxonomy : page([], { active_model: null })));
    const b = withClient(<Label />);
    expect(await screen.findByText(/No model yet/)).toBeInTheDocument();
    b.unmount();
    mockApi((url) => (url.includes("/taxonomy") ? taxonomy : fail(500, "internal_error", "Unexpected server error.")));
    withClient(<Label />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Unexpected server error.");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("flags stale suggestions and review items in words, not colour", async () => {
    const old = item(10, "old one", {
      scored_by_model: 5,
      review_reason: "split:hotel/rooms",
      current_node_ids: [1, 2],
    });
    mockApi((url) => (url.includes("/taxonomy") ? taxonomy : page([old])));
    withClient(<Label />);
    await screen.findByRole("heading", { name: "old one" });
    expect(screen.getByText(/Older suggestions/)).toBeInTheDocument();
    expect(screen.getByText(/Flagged by a taxonomy change/)).toBeInTheDocument();
  });

  it("search adds a category outside the suggestions together with its ancestors", async () => {
    mockApi((url) => (url.includes("/taxonomy") ? taxonomy : page([item(10, "dusty room", { suggestions: [] })])));
    const user = userEvent.setup();
    withClient(<Label />);
    await screen.findByRole("heading", { name: "dusty room" });
    expect(screen.getByText(/No suggestions for this item/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Add another category"), "clean");
    await user.click(screen.getByRole("button", { name: "hotel/rooms/cleanliness" }));
    const chips = screen.getByText(/Also selected/);
    const names = within(chips)
      .getAllByRole("button")
      .map((b) => b.textContent?.split(" ")[0]);
    expect(names.sort()).toEqual(["hotel", "hotel/rooms", "hotel/rooms/cleanliness"]);
  });
});

const metric = (path: string, support: number, f1: number): NodeMetric => ({
  node_id: path.length,
  path,
  title: path,
  depth: 1,
  parent_id: null,
  support,
  predicted: support,
  precision: f1,
  recall: f1,
  f1,
  precision_ci: [0, 1],
  recall_ci: [0, 1],
});

describe("Performance", () => {
  it("only calls a node weak when there is enough evidence", () => {
    expect(isWeak(metric("a", 500, 0.2))).toBe(true);
    expect(isWeak(metric("b", 5, 0.0))).toBe(false);
    expect(isWeak(metric("c", 500, 0.8))).toBe(false);
  });

  it("marks weak branches with text and filters to them", async () => {
    const summary = { n: 100, hf1: 0.61, precision: 0.6, recall: 0.62, exact_match: 0.2, macro_f1: 0.3 };
    mockApi((url) =>
      url.includes("/metrics/nodes")
        ? {
            body: {
              model_version: 7,
              lang: "all",
              languages: ["en", "sw"],
              overall: summary,
              by_lang: { en: summary, sw: { ...summary, hf1: 0.48 } },
              calibration: {},
              nodes: [metric("hotel", 900, 0.8), metric("hotel-pool", 300, 0.1)],
            },
          }
        : { body: { live: [], simulation: null } },
    );
    const user = userEvent.setup();
    withClient(<Performance />);
    expect(await screen.findByText("0.610")).toBeInTheDocument();
    expect(screen.getAllByText("▲ weak")).toHaveLength(1);
    await user.click(screen.getByRole("checkbox", { name: /Weak branches only/ }));
    expect(screen.queryByRole("rowheader", { name: "hotel" })).not.toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: /hotel-pool/ })).toBeInTheDocument();
    expect(await screen.findByText(/No models have been trained yet/)).toBeInTheDocument();
  });

  it("explains the no-model state instead of showing an error", async () => {
    mockApi((url) =>
      url.includes("/metrics/nodes")
        ? fail(404, "not_found", "No such model version")
        : { body: { live: [], simulation: null } },
    );
    withClient(<Performance />);
    expect(await screen.findByText(/No model yet/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("App", () => {
  it("rejects a bad token with a field error", async () => {
    mockApi(() => fail(401, "unauthenticated", "Unknown or revoked token."));
    const user = userEvent.setup();
    withClient(<App />);
    await user.type(screen.getByLabelText("Access token"), "nope{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("That token was not recognised.");
    expect(screen.getByLabelText("Access token")).toBeInvalid();
    expect(sessionStorage.getItem("parse.token")).toBeNull();
  });

  it("hides the labelling tab from a viewer", async () => {
    sessionStorage.setItem("parse.token", "t");
    mockApi((url) => (url.endsWith("/me") ? { body: { name: "v", role: "viewer" } } : undefined));
    withClient(<App />);
    expect(await screen.findByRole("button", { name: "Performance" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Label" })).not.toBeInTheDocument();
  });
});
