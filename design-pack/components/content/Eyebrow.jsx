import React from 'react';

/**
 * Eyebrow — uppercase section label with an optional numbered prefix.
 * The small terracotta kicker above section titles.
 */
export function Eyebrow({ children, number, color = 'var(--text-accent)', style = {}, ...rest }) {
  return (
    <div
      style={{
        fontFamily: 'var(--font-display)',
        fontWeight: 'var(--fw-semibold)',
        fontSize: 'var(--fs-micro)',
        letterSpacing: 'var(--ls-eyebrow)',
        textTransform: 'uppercase',
        color,
        ...style,
      }}
      {...rest}
    >
      {number ? `${number} — ` : ''}{children}
    </div>
  );
}
