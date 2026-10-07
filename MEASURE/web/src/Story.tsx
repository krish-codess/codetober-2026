import { AnimatePresence, animate, motion, useReducedMotion } from "framer-motion";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, createShare, recordView, type Card, type Share, type Wrapped } from "./api";

const CARD_SECONDS = 7;

/** A card the UI can show: it needs something to say. Anything else in a payload is skipped, not fatal. */
export function usableCards(cards: unknown): Card[] {
  if (!Array.isArray(cards)) return [];
  return cards.filter(
    (c): c is Card => !!c && typeof c === "object" && typeof c.type === "string" && typeof c.headline === "string" && c.headline !== "",
  );
}

/** Counts up to a whole number, then shows the server's exact string. Anything non-numeric just appears. */
function Value({ text, still }: { text: string; still: boolean }) {
  const target = /^\d[\d,]*$/.test(text) ? Number(text.replaceAll(",", "")) : null;
  const [shown, setShown] = useState(still || target === null ? text : "0");
  useEffect(() => {
    if (still || target === null) return setShown(text);
    const controls = animate(0, target, {
      duration: 1.1,
      delay: 0.35,
      ease: "easeOut",
      onUpdate: (v) => setShown(Math.round(v).toLocaleString("en-US")),
      onComplete: () => setShown(text),
    });
    return () => controls.stop();
  }, [text, still, target]);
  return (
    <>
      <span aria-hidden="true">{shown}</span>
      <span className="sr-only">{text}</span>
    </>
  );
}

function CardView({ card, still, focus }: { card: Card; still: boolean; focus: boolean }) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  // After a move, put focus on the new heading so keyboard and screen-reader users land on the content.
  // Done here, on mount, because the card only mounts once the previous one has finished leaving.
  useEffect(() => {
    if (focus) headingRef.current?.focus();
  }, [focus]);
  // Paced reveal: each line arrives a beat after the one before. With reduced motion, everything is simply there.
  const reveal = (order: number) =>
    still
      ? {}
      : { initial: { opacity: 0, y: 18 }, animate: { opacity: 1, y: 0 }, transition: { delay: 0.12 + order * 0.38, duration: 0.5 } };
  return (
    <div className="card-body">
      {card.family !== "frame" && (
        <motion.p className="family" {...reveal(0)}>
          {card.family}
        </motion.p>
      )}
      <motion.h2 ref={headingRef} tabIndex={-1} {...reveal(0)}>
        {card.headline}
      </motion.h2>
      {card.value && (
        <motion.p className="value" {...reveal(1)}>
          <Value text={card.value} still={still} />
        </motion.p>
      )}
      {card.unit && (
        <motion.p className="unit" {...reveal(2)}>
          {card.unit}
        </motion.p>
      )}
      {card.stats && card.stats.length > 0 && (
        <motion.dl className="stats" {...reveal(2)}>
          {card.stats.map((s) => (
            <div key={s.label}>
              <dt>{s.label}</dt>
              <dd>{s.value}</dd>
            </div>
          ))}
        </motion.dl>
      )}
      {card.body && (
        <motion.p className="body" {...reveal(3)}>
          {card.body}
        </motion.p>
      )}
      {card.claim && (
        <motion.div
          className="claim"
          {...(still ? {} : { initial: { opacity: 0, scale: 0.8 }, animate: { opacity: 1, scale: 1 }, transition: { delay: 1.7, type: "spring", bounce: 0.5 } })}
        >
          <strong>{card.claim.text}</strong>
          <span>{card.claim.basis}</span>
        </motion.div>
      )}
    </div>
  );
}

type ShareState = { step: "creating" } | { step: "rendering" | "done"; share: Share } | { step: "failed"; message: string };

function ShareSheet({ token, card, onClose }: { token: string; card: Card; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [state, setState] = useState<ShareState>({ step: "creating" });
  const [copied, setCopied] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    ref.current?.showModal(); // native modal: focus is trapped, Escape closes, focus returns to the opener
  }, []);
  useEffect(() => {
    let cancelled = false;
    setState({ step: "creating" });
    createShare(token, card.type)
      .then((share) => !cancelled && setState({ step: "rendering", share }))
      .catch((err) => !cancelled && setState({ step: "failed", message: err instanceof ApiError ? err.message : "Sharing failed." }));
    return () => {
      cancelled = true;
    };
  }, [token, card.type, attempt]);

  const share = "share" in state ? state.share : null;
  const steps = { creating: 1, rendering: 2, done: 3, failed: 0 }[state.step];
  return (
    <dialog ref={ref} className="sheet" aria-labelledby="sheet-title" onClose={onClose}>
      <h2 id="sheet-title">Share this card</h2>
      {state.step === "failed" ? (
        <div role="alert">
          <p>{state.message}</p>
          <button type="button" onClick={() => setAttempt((a) => a + 1)}>
            Try again
          </button>
        </div>
      ) : (
        <>
          {state.step !== "done" && (
            <p className="progress-line">
              <progress max={3} value={steps} aria-label="Preparing your card" />
              <span role="status">{state.step === "creating" ? "Creating your link (1 of 2)" : "Drawing your card (2 of 2)"}</span>
            </p>
          )}
          {share && (
            <>
              <img
                className={state.step === "done" ? "preview" : "preview pending"}
                src={share.image_url}
                width={1200}
                height={630}
                alt={`Share card: ${card.headline}. ${card.value} ${card.unit}. ${card.claim?.text ?? ""}`}
                onLoad={() => setState({ step: "done", share })}
                onError={() => setState({ step: "failed", message: "The card image could not be loaded." })}
              />
              <div className="sheet-actions">
                <button
                  type="button"
                  onClick={() => navigator.clipboard?.writeText(share.url).then(() => setCopied(true), () => setCopied(false))}
                >
                  {copied ? "Link copied" : "Copy link"}
                </button>
                <a className="button" href={share.image_url} download={`wrapped-${card.type}.png`}>
                  Download image
                </a>
                {typeof navigator.share === "function" && (
                  <button type="button" onClick={() => navigator.share({ title: card.headline, url: share.url }).catch(() => {})}>
                    Share…
                  </button>
                )}
              </div>
              <p className="share-url">
                <a href={share.url}>{share.url}</a>
              </p>
            </>
          )}
        </>
      )}
      <button type="button" className="quiet" onClick={() => ref.current?.close()}>
        Close
      </button>
    </dialog>
  );
}

export function Story({ token, wrapped, stale, savedAt, onReload }: { token: string; wrapped: Wrapped; stale: boolean; savedAt: number; onReload: () => void }) {
  const cards = useMemo(() => usableCards(wrapped.cards), [wrapped.cards]);
  const reduced = useReducedMotion() ?? false;
  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(!reduced);
  const [sharing, setSharing] = useState(false);
  const seen = useRef(new Set<string>());
  const moved = useRef(false);
  const last = cards.length - 1;
  const card = cards[Math.min(index, last)];

  const go = useCallback(
    (to: number) => {
      moved.current = true;
      setIndex(Math.max(0, Math.min(last, to)));
    },
    [last],
  );

  useEffect(() => {
    if (!card || seen.current.has(card.type)) return;
    seen.current.add(card.type);
    recordView(token, card.type);
  }, [card, token]);

  useEffect(() => {
    if (sharing) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.altKey || e.ctrlKey || e.metaKey) return;
      const typing = e.target instanceof HTMLElement && ["BUTTON", "A", "INPUT"].includes(e.target.tagName);
      if (e.key === "ArrowRight") go(index + 1);
      else if (e.key === "ArrowLeft") go(index - 1);
      else if (e.key === "Home") go(0);
      else if (e.key === "End") go(last);
      else if (e.key === " " && !typing) setPlaying((p) => !p);
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [go, index, last, sharing]);

  if (!card) return null;
  const running = playing && !sharing && index < last;
  return (
    <main className="story" data-family={card.family}>
      {stale && (
        <p className="stale" role="status">
          Showing a copy saved {new Date(savedAt).toLocaleString()}. We could not reach the server.{" "}
          <button type="button" className="link" onClick={onReload}>
            Try again
          </button>
        </p>
      )}
      <ol className="segments" aria-label="Story progress">
        {cards.map((c, i) => (
          <li key={c.type} className={i < index || (i === index && index === last) ? "past" : i === index ? "current" : ""} aria-current={i === index ? "step" : undefined}>
            <span className="sr-only">
              Card {i + 1} of {cards.length}
            </span>
            {i === index && index < last && (
              <i
                key={`${index}-${running}`}
                style={{ animationDuration: `${CARD_SECONDS}s`, animationPlayState: running ? "running" : "paused" }}
                onAnimationEnd={() => go(index + 1)}
              />
            )}
          </li>
        ))}
      </ol>
      <header>
        <span className="brand">Wrapped {wrapped.year}</span>
        <span className="login">@{wrapped.user.login}</span>
      </header>

      {/* Tap zones for touch. The buttons below do the same job for keyboard and assistive tech. */}
      <div className="tap prev" aria-hidden="true" onClick={() => go(index - 1)} />
      <div className="tap next" aria-hidden="true" onClick={() => go(index + 1)} />

      <AnimatePresence mode="wait" initial={false}>
        <motion.section
          key={card.type}
          className="card"
          aria-label={`Card ${index + 1} of ${cards.length}: ${card.headline}`}
          {...(reduced ? {} : { initial: { opacity: 0, x: 40 }, animate: { opacity: 1, x: 0 }, exit: { opacity: 0, x: -40 }, transition: { duration: 0.25 } })}
        >
          <CardView card={card} still={reduced} focus={moved.current} />
        </motion.section>
      </AnimatePresence>

      <nav className="controls" aria-label="Story controls">
        <button type="button" onClick={() => go(index - 1)} disabled={index === 0}>
          Previous
        </button>
        <button type="button" onClick={() => setPlaying((p) => !p)} aria-pressed={playing} disabled={index === last}>
          {playing ? "Pause" : "Play"}
        </button>
        <span className="count" aria-live="polite">
          {index + 1} / {cards.length}
        </span>
        {card.shareable && (
          <button type="button" className="primary" onClick={() => setSharing(true)}>
            Share
          </button>
        )}
        <button type="button" onClick={() => go(index + 1)} disabled={index === last}>
          Next
        </button>
      </nav>
      {sharing && <ShareSheet token={token} card={card} onClose={() => setSharing(false)} />}
    </main>
  );
}
