// Shimmer skeletons — shown while a screen's first data loads, so a driver sees
// the *shape* of what's coming instead of a bare spinner or a blank panel.

export function SkeletonLine({ width = "100%", lg = false }: { width?: string; lg?: boolean }) {
  return <div className={`skeleton sk-line ${lg ? "lg" : ""}`} style={{ width }} />;
}

// A card-shaped placeholder that mirrors the Home "current trip" block.
export function SkeletonCard({ lines = 3 }: { lines?: number }) {
  return (
    <div className="skeleton-card">
      <SkeletonLine width="45%" />
      <SkeletonLine width="80%" lg />
      {Array.from({ length: Math.max(0, lines - 2) }).map((_, i) => (
        <SkeletonLine key={i} width={`${70 - i * 12}%`} />
      ))}
    </div>
  );
}

// A row of stat-tile placeholders.
export function SkeletonStats({ count = 3 }: { count?: number }) {
  return (
    <div className="stat-row" style={{ marginBottom: 12 }}>
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="stat">
          <div className="skeleton sk-line lg" style={{ width: "60%", margin: "0 auto 8px" }} />
          <div className="skeleton sk-line" style={{ width: "80%", margin: "0 auto" }} />
        </div>
      ))}
    </div>
  );
}
