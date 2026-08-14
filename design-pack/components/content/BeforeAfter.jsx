import React from 'react';

/**
 * BeforeAfter — "до / после" contrast scheme.
 * Two tiles (neutral → graphite) split by a terracotta arrow.
 */
export function BeforeAfter({
  beforeLabel = 'Без AI',
  beforeValue,
  afterLabel = 'С AI',
  afterValue,
  style = {},
}) {
  const tile = {
    flex: 1,
    borderRadius: 'var(--radius-md)',
    padding: '18px',
  };
  const kicker = {
    fontSize: '11px',
    fontWeight: 'var(--fw-semibold)',
    textTransform: 'uppercase',
    letterSpacing: '0.05em',
  };
  const val = {
    fontFamily: 'var(--font-display)',
    fontWeight: 'var(--fw-semibold)',
    fontSize: '15px',
    marginTop: '8px',
    lineHeight: 1.35,
  };
  return (
    <div style={{ display: 'flex', alignItems: 'stretch', gap: '14px', ...style }}>
      <div style={{ ...tile, background: 'var(--bg-section-alt)' }}>
        <div style={{ ...kicker, color: 'var(--text-secondary)' }}>{beforeLabel}</div>
        <div style={val}>{beforeValue}</div>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', fontFamily: 'var(--font-display)', fontWeight: 'var(--fw-bold)', fontSize: '22px', color: 'var(--accent)' }}>
        →
      </div>
      <div style={{ ...tile, background: 'var(--surface-inverse)', color: 'var(--text-on-dark)' }}>
        <div style={{ ...kicker, color: 'var(--accent)' }}>{afterLabel}</div>
        <div style={val}>{afterValue}</div>
      </div>
    </div>
  );
}
