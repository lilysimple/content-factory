import React from 'react';

/**
 * QuoteCard — prompt / quote card on a soft sage tint.
 * Large Manrope quote with an optional author row (avatar + label).
 */
export function QuoteCard({
  children,             // the quote / prompt text
  eyebrow = 'Промпт',   // small uppercase kicker
  author,               // author label
  avatar,               // avatar image src
  style = {},
}) {
  return (
    <div
      style={{
        background: 'var(--sage-tint)',
        border: '1px solid var(--sage-tint-line)',
        borderRadius: 'var(--radius-card)',
        padding: 'var(--card-pad)',
        ...style,
      }}
    >
      {eyebrow && (
        <div style={{
          fontFamily: 'var(--font-display)', fontSize: '11.5px', fontWeight: 'var(--fw-semibold)',
          letterSpacing: 'var(--ls-label)', textTransform: 'uppercase', color: 'var(--sage-deep)', marginBottom: '18px',
        }}>{eyebrow}</div>
      )}
      <div style={{
        fontFamily: 'var(--font-display)', fontWeight: 'var(--fw-bold)', fontSize: '22px',
        lineHeight: 1.32, letterSpacing: '-0.015em', color: 'var(--text-primary)',
      }}>{children}</div>
      {author && (
        <div style={{ marginTop: '20px', display: 'flex', alignItems: 'center', gap: '10px' }}>
          {avatar && (
            <div style={{ width: '30px', height: '30px', borderRadius: '50%', overflow: 'hidden', flex: 'none' }}>
              <img src={avatar} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
            </div>
          )}
          <span style={{ fontSize: '13px', color: 'var(--text-secondary)', fontWeight: 'var(--fw-medium)' }}>{author}</span>
        </div>
      )}
    </div>
  );
}
