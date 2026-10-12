import { useEffect, useRef, useState } from "react";
import { Investigation } from "./Investigation";
import { LiveMap } from "./LiveMap";

/** #/vessel/257000001 -> 257000001; anything else is the live map. */
export function routeOf(hash: string): number | null {
  const m = /^#\/vessel\/(\d{9})$/.exec(hash);
  return m ? Number(m[1]) : null;
}

export function App() {
  const [mmsi, setMmsi] = useState(() => routeOf(location.hash));
  const heading = useRef<HTMLHeadingElement>(null);
  const first = useRef(true);
  useEffect(() => {
    const onHash = () => setMmsi(routeOf(location.hash));
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);
  useEffect(() => {
    document.title = mmsi ? `Vessel ${mmsi} · Ghost Fleet` : "Ghost Fleet";
    // After a view change, put keyboard and screen-reader focus at the top of the new view.
    if (!first.current) heading.current?.focus();
    first.current = false;
  }, [mmsi]);
  const go = (target: number | null) => {
    location.hash = target ? `#/vessel/${target}` : "#/";
  };
  return (
    <>
      <header>
        <h1 ref={heading} tabIndex={-1}>
          Ghost Fleet <small>{mmsi ? `investigating ${mmsi}` : "live"}</small>
        </h1>
      </header>
      <main>{mmsi ? <Investigation key={mmsi} mmsi={mmsi} onBack={() => go(null)} /> : <LiveMap onOpenVessel={go} />}</main>
    </>
  );
}
