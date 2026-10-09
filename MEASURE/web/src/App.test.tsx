import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import type { Card, Wrapped } from "./api";

const card = (over: Partial<Card>): Card => ({
  type: "streak", family: "consistency", shareable: true, headline: "Your longest streak", value: "23", unit: "days in a row",
  body: "From 1 March to 23 March, you did not miss a day.", claim: null, ...over,
}); // prettier-ignore

const story: Wrapped = {
  version: 1, year: 2025, user: { id: 7, login: "amber-otter" }, tier: "full", archetype: "The Marathoner",
  population: { users: 3002, basis: "people active this year" }, run_id: "r1", generated_at: "2026-01-02T00:00:00Z",
  cards: [
    card({ type: "intro", family: "frame", shareable: false, headline: "Your 2025", value: "amber-otter", unit: "", body: "Here is what stood out." }),
    card({ claim: { metric: "longest_streak_days", top_permille: 50, text: "Top 5% for longest streak", basis: "among 3,002 people active this year" } }),
    card({ type: "summary", family: "frame", headline: "The Marathoner", value: "amber-otter", unit: "2025", body: "That was your 2025.", stats: [{ label: "events", value: "1,204" }] }),
  ],
}; // prettier-ignore

type Route = (init: RequestInit) => Response | Promise<Response>;
const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(status === 204 || status === 304 ? null : JSON.stringify(body), { status, headers });
const apiError = (status: number, code: string, message: string) => json({ error: { code, message, request_id: "req-123" } }, status);

/** Replace fetch with a table of "METHOD path" handlers and return the calls that were made. */
function serve(routes: Record<string, Route>) {
  const calls: string[] = [];
  vi.stubGlobal("fetch", async (url: string, init: RequestInit = {}) => {
    const key = `${init.method ?? "GET"} ${url}`;
    calls.push(key);
    const route = routes[key] ?? routes[key.replace(/\/views\/.*/, "/views/*")];
    if (!route) throw new TypeError(`no route for ${key}`);
    return route(init);
  });
  return calls;
}

const ok: Record<string, Route> = {
  "GET /v1/wrapped": () => json(story, 200, { ETag: '"r1-7"' }),
  "PUT /v1/wrapped/views/*": () => json(null, 204),
};

function open(token = "tok.sig") {
  window.location.hash = `#t=${token}`;
  return render(<App />);
}

const heading = (name: string) => screen.findByRole("heading", { name });

test("shows determinate progress, then the first card, and takes the token out of the address bar", async () => {
  let release!: (r: Response) => void;
  serve({ ...ok, "GET /v1/wrapped": () => new Promise<Response>((r) => (release = r)) });
  open();
  const progress = screen.getByRole("progressbar", { name: "Loading your story" });
  expect(progress).toHaveAttribute("value", "1");
  expect(screen.getByRole("status")).toHaveTextContent("Finding your year (1 of 3)");
  expect(window.location.hash).toBe("");
  await act(async () => release(json(story, 200, { ETag: '"r1-7"' })));
  expect(await heading("Your 2025")).toBeInTheDocument();
  expect(screen.getByText("@amber-otter")).toBeInTheDocument();
});

test("keyboard moves through the story and focus follows the content", async () => {
  const calls = serve(ok);
  const user = userEvent.setup();
  open();
  await heading("Your 2025");
  expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();

  await user.keyboard("{ArrowRight}");
  const streak = await heading("Your longest streak");
  await waitFor(() => expect(streak).toHaveFocus());
  expect(screen.getByText("Top 5% for longest streak")).toBeInTheDocument();
  expect(screen.getByText("among 3,002 people active this year")).toBeInTheDocument();
  expect(screen.getByText("2 / 3")).toBeInTheDocument();

  await user.keyboard("{End}");
  expect(await heading("The Marathoner")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  await user.keyboard("{ArrowRight}{ArrowRight}"); // past the end: stays put
  expect(screen.getByText("3 / 3")).toBeInTheDocument();
  await user.keyboard("{Home}");
  expect(await heading("Your 2025")).toBeInTheDocument();

  // Each card's view is recorded once, however often it is revisited.
  await user.keyboard("{ArrowRight}{ArrowLeft}{ArrowRight}");
  expect(calls.filter((c) => c.startsWith("PUT")).sort()).toEqual([
    "PUT /v1/wrapped/views/intro", "PUT /v1/wrapped/views/streak", "PUT /v1/wrapped/views/summary",
  ]); // prettier-ignore
});

test("buttons work without a keyboard shortcut, and only shareable cards offer sharing", async () => {
  serve(ok);
  const user = userEvent.setup();
  open();
  await heading("Your 2025");
  expect(screen.queryByRole("button", { name: "Share" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Next" }));
  await heading("Your longest streak");
  expect(screen.getByRole("button", { name: "Share" })).toBeInTheDocument();
  const pause = screen.getByRole("button", { name: /Pause|Play/ });
  const pressed = pause.getAttribute("aria-pressed");
  await user.click(pause);
  expect(pause).not.toHaveAttribute("aria-pressed", pressed!);
});

test("the count-up ends on exactly what the server sent", async () => {
  serve(ok);
  const user = userEvent.setup();
  open();
  await heading("Your 2025");
  await user.keyboard("{End}");
  await heading("The Marathoner");
  expect(screen.getByText("1,204")).toBeInTheDocument();
  await user.keyboard("{ArrowLeft}");
  await heading("Your longest streak");
  // Screen readers get the final value immediately; the animated digits are hidden from them.
  expect(screen.getByText("23", { selector: ".sr-only" })).toBeInTheDocument();
  await waitFor(() => expect(screen.getByText("23", { selector: "[aria-hidden]" })).toBeInTheDocument(), { timeout: 3000 });
});

test("sharing creates one share, shows real progress, then the card and its link", async () => {
  const share = { share_id: "s".repeat(22), card_type: "streak", url: "https://x.example/s/abc", image_url: "/v1/shares/abc/card.png?v=1", created: true };
  let release!: (r: Response) => void;
  const calls = serve({ ...ok, "POST /v1/wrapped/shares": () => new Promise<Response>((r) => (release = r)) });
  const user = userEvent.setup();
  open();
  await heading("Your 2025");
  await user.keyboard("{ArrowRight}");
  await user.click(await screen.findByRole("button", { name: "Share" }));

  const sheet = screen.getByRole("dialog", { name: "Share this card" });
  expect(within(sheet).getByRole("status")).toHaveTextContent("Creating your link (1 of 2)");
  await act(async () => release(json(share, 201)));
  expect(await within(sheet).findByRole("status")).toHaveTextContent("Drawing your card (2 of 2)");
  const image = within(sheet).getByRole("img");
  expect(image).toHaveAttribute("src", share.image_url);
  expect(image).toHaveAccessibleName(/Your longest streak\. 23 days in a row\. Top 5%/);
  act(() => image.dispatchEvent(new Event("load")));
  await waitFor(() => expect(within(sheet).queryByRole("status")).not.toBeInTheDocument());
  expect(within(sheet).getByRole("link", { name: share.url })).toHaveAttribute("href", share.url);
  expect(within(sheet).getByRole("link", { name: "Download image" })).toHaveAttribute("download", "wrapped-streak.png");
  expect(calls.filter((c) => c.startsWith("POST"))).toHaveLength(1);

  // Arrow keys belong to the dialog while it is open.
  await user.keyboard("{ArrowRight}");
  expect(screen.getByText("2 / 3")).toBeInTheDocument();
  await user.click(within(sheet).getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
});

test("a failed share says why and can be retried", async () => {
  let attempts = 0;
  serve({
    ...ok,
    "POST /v1/wrapped/shares": () =>
      ++attempts === 1
        ? apiError(503, "database_unavailable", "The service is temporarily unable to reach its database.")
        : json({ share_id: "s".repeat(22), card_type: "streak", url: "https://x.example/s/abc", image_url: "/img.png", created: true }, 201),
  });
  const user = userEvent.setup();
  open();
  await heading("Your 2025");
  await user.keyboard("{ArrowRight}");
  await user.click(await screen.findByRole("button", { name: "Share" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("temporarily unable to reach its database");
  await user.click(screen.getByRole("button", { name: "Try again" }));
  expect(await screen.findByRole("img")).toBeInTheDocument();
});

test("empty: a valid link for an account with no activity gets a kind page, not an error", async () => {
  serve({ "GET /v1/wrapped": () => apiError(404, "wrapped_not_found", "There is no 2025 story for this account.") });
  open();
  expect(await heading("No story this year")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test("unauthorized: an expired link explains itself and lets the user enter another", async () => {
  serve({ "GET /v1/wrapped": () => apiError(401, "invalid_token", "A valid personal link is required.") });
  const user = userEvent.setup();
  open();
  expect(await heading("This link no longer works")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Use a different link" }));
  expect(await screen.findByLabelText("Personal link or code")).toBeInTheDocument();
});

test("error: transient failures are retried with backoff, then handed to the user with a request id", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  let fail = true;
  const calls = serve({
    ...ok,
    "GET /v1/wrapped": () => (fail ? apiError(503, "database_unavailable", "The service is temporarily unable to reach its database.") : json(story)),
  });
  open();
  await screen.findByRole("status");
  await act(() => vi.advanceTimersByTimeAsync(1100));
  expect(screen.getByRole("status")).toHaveTextContent("Attempt 2 of 4");
  await act(() => vi.advanceTimersByTimeAsync(7000));
  expect(await screen.findByRole("alert")).toHaveTextContent("temporarily unable to reach its database");
  expect(screen.getByText(/quote request req-123/)).toBeInTheDocument();
  expect(calls.filter((c) => c === "GET /v1/wrapped")).toHaveLength(4);

  fail = false;
  vi.useRealTimers();
  await userEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(await heading("Your 2025")).toBeInTheDocument();
});

test("a permanent error is not retried", async () => {
  const calls = serve({ "GET /v1/wrapped": () => apiError(422, "invalid_request", "The request did not match the contract.") });
  open();
  expect(await screen.findByRole("alert")).toHaveTextContent("did not match the contract");
  expect(calls).toHaveLength(1);
});

test("cache: a recent copy is shown with no request at all", async () => {
  const calls = serve(ok);
  const first = open();
  await heading("Your 2025");
  first.unmount();
  const before = calls.filter((c) => c.startsWith("GET")).length;
  open();
  expect(await heading("Your 2025")).toBeInTheDocument();
  expect(calls.filter((c) => c.startsWith("GET"))).toHaveLength(before);
});

test("stale: an old copy stays on screen when the server is unreachable, labelled, and can be refreshed", async () => {
  const saved = { wrapped: story, etag: '"r1-7"', savedAt: Date.now() - 3_600_000 };
  localStorage.setItem("wrapped:v1:tok.sig", JSON.stringify(saved));
  let online = false;
  let sentEtag: string | undefined;
  serve({
    ...ok,
    "GET /v1/wrapped": (init) => {
      sentEtag = (init.headers as Record<string, string>)["If-None-Match"];
      if (!online) throw new TypeError("Failed to fetch");
      return json(null, 304);
    },
  });
  const user = userEvent.setup();
  open();
  expect(await heading("Your 2025")).toBeInTheDocument(); // immediately, from the saved copy
  const banner = await screen.findByText(/Showing a copy saved/);
  expect(banner).toHaveTextContent("We could not reach the server");
  expect(sentEtag).toBe('"r1-7"');

  online = true;
  await user.click(within(banner).getByRole("button", { name: "Try again" }));
  await waitFor(() => expect(screen.queryByText(/Showing a copy saved/)).not.toBeInTheDocument());
  expect(screen.getByRole("heading", { name: "Your 2025" })).toBeInTheDocument();
});

test("partial data: unknown card types render, broken cards are skipped, and nothing usable is its own state", async () => {
  const odd = { ...story, cards: [story.cards[0], { type: "from_the_future", family: "novel", shareable: false, headline: "A card this build has never heard of", value: "42", unit: "", body: "", claim: null }, null, { type: "broken" }, "nonsense"] };
  serve({ ...ok, "GET /v1/wrapped": () => json(odd) });
  const user = userEvent.setup();
  const first = open();
  await heading("Your 2025");
  expect(screen.getByText("1 / 2")).toBeInTheDocument();
  await user.keyboard("{ArrowRight}");
  expect(await heading("A card this build has never heard of")).toBeInTheDocument();
  first.unmount();
  localStorage.clear();

  serve({ ...ok, "GET /v1/wrapped": () => json({ ...story, cards: [{ type: "broken" }] }) });
  open("other.tok");
  expect(await heading("Your story is not ready")).toBeInTheDocument();
});

test("with no link, the landing page asks for one and accepts a pasted link", async () => {
  serve(ok);
  const user = userEvent.setup();
  render(<App />);
  await user.type(screen.getByLabelText("Personal link or code"), "https://app.example/#t=tok.sig");
  await user.click(screen.getByRole("button", { name: "Show my year" }));
  expect(await heading("Your 2025")).toBeInTheDocument();
});

test("admin: sign-in, both analyses with numbers in text, and a rejected token", async () => {
  serve({
    "GET /v1/admin/analytics/superlatives": (init) =>
      (init.headers as Record<string, string>).Authorization === "Bearer good"
        ? json({ run_id: "abcdef123456", users: 200, cards: [{ card_type: "streak", family: "consistency", users: 50, share_of_users: 0.25 }] })
        : apiError(403, "forbidden", "This token does not grant admin access."),
    "GET /v1/admin/analytics/share-rate": (init) =>
      (init.headers as Record<string, string>).Authorization === "Bearer good"
        ? json([{ card_type: "streak", viewers: 40, sharers: 10, share_rate: 0.25 }, { card_type: "summary", viewers: 0, sharers: 0, share_rate: null }])
        : apiError(403, "forbidden", "This token does not grant admin access."),
  });
  const user = userEvent.setup();
  window.location.hash = "#admin";
  render(<App />);
  await user.type(screen.getByLabelText("Admin token"), "wrong");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("was not accepted");

  await user.type(screen.getByLabelText("Admin token"), "good");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  const dist = within(await screen.findByRole("region", { name: "Superlative distribution" }));
  expect(dist.getByRole("row", { name: /streak consistency 50 25\.0%/ })).toBeInTheDocument();
  const rate = within(screen.getByRole("region", { name: "Share rate by card type" }));
  expect(rate.getByRole("row", { name: /streak 40 10 25\.0%/ })).toBeInTheDocument();
  expect(rate.getByRole("row", { name: /summary 0 0 no views yet/ })).toBeInTheDocument();
});
