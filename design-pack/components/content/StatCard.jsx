import React from 'react';

/**
 * StatCard — dark metric / infographic card.
 * Big terracotta-accented number with an optional progress bar and range labels.
 */
export function StatCard({
  value,                 // headline figure, e.g. "−40%"
  label,                 // supporting sentence
  eyebrow,               // small uppercase kicker (terracotta)
  progress,              // 0–100, optional bar fill
  from,                  // left range label
  to,                    // right range label
  style = {},
}) {
  return (
    <div
      style={{
        background: 'var(--surface-inverse)',
        borderRadius: 'var(--radius-card)',
        padding: 'var(--card-pad)',
        color: 'var(--text-on-dark)',
        ...style,
      }}
    >
      {eyebrow && (
        <div style={{
          fontFamily: 'var(--font-display)', fontSize: '11.5px', fontWeight: 'var(--fw-semibold)',
          letterSpacing: 'var(--ls-label)', textTransform: 'uppercase', color: 'var(--accent)', marginBottom: '18px',
        }}>{eyebrow}</div>
      )}
      <div style={{
        fontFamily: 'var(--font-display)', fontWeight: 'var(--fw-extra)', fontSize: '58px',
        lineHeight: 0.95, letterSpacing: 'var(--ls-display)',
      }}>{value}</div>
      {label && (
        <div style={{ fontFamily: 'var(--font-body)', fontSize: '14px', color: 'var(--text-on-dark-2)', marginTop: '8px', lineHeight: 1.4 }}>
          {label}
        </div>
      )}
      {progress != null && (
        <>
          <div style={{ marginTop: '22px', height: '8px', borderRadius: 'var(--radius-pill)', background: 'rgba(248,245,241,0.14)', overflow: 'hidden' }}>
            <div style={{ width: `${progress}%`, height: '100%', background: 'var(--accent)', borderRadius: 'var(--radius-pill)' }} />
          </div>
          {(from || to) && (
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '11.5px', color: 'var(--on-dark-soft)', marginTop: '8px' }}>
              <span>{from}</span><span>{to}</span>
            </div>
          )}
        </>
      )}
    </div>
  );
}
