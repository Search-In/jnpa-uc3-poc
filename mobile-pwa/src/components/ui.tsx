import type { ReactNode } from "react";

export function Spinner() {
  return <span className="spinner" aria-label="loading" />;
}

export function Card({
  title,
  children,
  className = "",
}: {
  title?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`card ${className}`}>
      {title ? <div className="card-title">{title}</div> : null}
      {children}
    </div>
  );
}

export function Stat({
  value,
  unit,
  label,
}: {
  value: ReactNode;
  unit?: string;
  label: string;
}) {
  return (
    <div className="stat">
      <div className="v">
        {value}
        {unit ? <span className="u">{unit}</span> : null}
      </div>
      <div className="l">{label}</div>
    </div>
  );
}

export function Chip({
  status = "ok",
  children,
}: {
  status?: "ok" | "warn" | "down" | "open" | "closed";
  children: ReactNode;
}) {
  return (
    <span className={`chip ${status}`}>
      <span className="dot" />
      {children}
    </span>
  );
}

export function Row({ k, v }: { k: ReactNode; v: ReactNode }) {
  return (
    <div className="row">
      <span className="k">{k}</span>
      <span className="v">{v}</span>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}
