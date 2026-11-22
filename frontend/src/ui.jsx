// Small shared formatting helpers and presentational components.

export const TYPE_LABELS = {
  DUPLICATE_PAYMENT: "Duplicate payment",
  PRICE_VARIANCE: "Price above contract",
  VOLUME_DISCOUNT: "Missed volume discount",
  EARLY_PAY_DISCOUNT: "Missed early-pay discount",
  DUPLICATE_VENDOR: "Duplicate vendor",
};

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

export const money = (v) => usd.format(v ?? 0);

export function moneyShort(v) {
  const a = Math.abs(v ?? 0);
  if (a >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
  if (a >= 1e4) return `$${(v / 1e3).toFixed(1)}K`;
  return money(v);
}

export const num = (v) => (v ?? 0).toLocaleString("en-US");

export function bytes(b) {
  if (b >= 1048576) return `${(b / 1048576).toFixed(1)} MB`;
  if (b >= 1024) return `${Math.round(b / 1024)} KB`;
  return `${b} B`;
}

export const pct = (v) => `${Math.round((v ?? 0) * 100)}%`;

export function TypeTag({ type }) {
  return <span className={`tag t-${type}`}>{TYPE_LABELS[type] || type}</span>;
}

export function Severity({ value }) {
  return <span className={`sev sev-${value}`}>{value}</span>;
}

export function Tile({ label, value, sub, tone }) {
  return (
    <div className={`tile ${tone ? `tile-${tone}` : ""}`}>
      <div className="tile-label">{label}</div>
      <div className="tile-value">{value}</div>
      {sub ? <div className="tile-sub">{sub}</div> : null}
    </div>
  );
}

/** Horizontal CSS bar rows: [{ key, label, amount, count, type? }] */
export function Bars({ rows, empty = "Nothing to show." }) {
  if (!rows?.length) return <p className="muted">{empty}</p>;
  const top = Math.max(...rows.map((r) => r.amount), 1);
  return (
    <div className="bars">
      {rows.map((r) => (
        <div className="bar-row" key={r.key || r.label}>
          <div className="bar-label">
            {r.label} <span className="muted">· {r.count}</span>
          </div>
          <div className="bar-track">
            <div className={`bar-fill ${r.type ? `fill-${r.type}` : ""}`} style={{ width: `${(r.amount / top) * 100}%` }} />
          </div>
          <div className="bar-value">{money(r.amount)}</div>
        </div>
      ))}
    </div>
  );
}

export function Spinner({ label }) {
  return (
    <div className="spinner-row">
      <span className="spinner" aria-hidden="true" />
      {label}
    </div>
  );
}

export function Alert({ tone = "error", title, children }) {
  return (
    <div className={`alert alert-${tone}`} role={tone === "error" ? "alert" : "status"}>
      {title ? <strong>{title}</strong> : null}
      {children ? <div>{children}</div> : null}
    </div>
  );
}

export function Ids({ ids, max = 12 }) {
  if (!ids?.length) return <span className="muted">none</span>;
  return (
    <span className="ids">
      {ids.slice(0, max).map((id) => (
        <code key={id}>{id}</code>
      ))}
      {ids.length > max ? <span className="muted">+{ids.length - max} more</span> : null}
    </span>
  );
}
