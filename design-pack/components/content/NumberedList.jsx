import React from 'react';

/**
 * NumberedList — ordered steps card with terracotta numerals.
 * items: [{ title, note }]
 */
export function NumberedList({ items = [], eyebrow, style = {} }) {
  return (
    <div
      style={{
        background: 'var(--surface-card)',
        border: 'var(--border)',
        borderRadius: 'var(--radius-card)',
        padding: '28px',
        ...style,
      }}
    >
      {eyebrow && (
        <div style={{
          fontSize: '11.5px', fontWeight: 'var(--fw-semibold)', letterSpacing: 'var(--ls-label)',
          textTransform: 'uppercase', color: 'var(--accent-2)', marginBottom: '18px',
        }}>{eyebrow}</div>
      )}
      {items.map((it, i) => (
        <div
          key={i}
          style={{
            display: 'flex', gap: '14px', alignItems: 'baseline',
            padding: `${i === 0 ? '0' : '14px'} 0 ${i === items.length - 1 ? '0' : '14px'}`,
            borderBottom: i === items.length - 1 ? 'none' : 'var(--border)',
          }}
        >
          <span style={{ fontFamily: 'var(--font-display)', fontWeight: 'var(--fw-bold)', color: 'var(--accent)', fontSize: '14px', flex: 'none' }}>
            {String(i + 1).padStart(2, '0')}
          </span>
          <div>
            <div style={{ fontFamily: 'var(--font-display)', fontWeight: 'var(--fw-semibold)', fontSize: '15.5px' }}>{it.title}</div>
            {it.note && <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginTop: '2px' }}>{it.note}</div>}
          </div>
        </div>
      ))}
    </div>
  );
}
