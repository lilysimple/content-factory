import React from 'react';

/**
 * RubricPill — content rubric / category tag.
 * Four tones matching the brand rubric system.
 */
export function RubricPill({
  children,
  tone = 'dark',   // 'dark' | 'outline' | 'sage' | 'terracotta'
  style = {},
  ...rest
}) {
  const tones = {
    dark:       { background: 'var(--surface-inverse)', color: 'var(--text-on-dark)', border: '1px solid transparent' },
    outline:    { background: 'var(--surface-card)', color: 'var(--text-primary)', border: '1px solid var(--border-firm)' },
    sage:       { background: 'var(--sage-tint)', color: 'var(--sage-deep)', border: '1px solid transparent' },
    terracotta: { background: 'var(--terracotta-tint)', color: 'var(--accent-hover)', border: '1px solid transparent' },
  };
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        padding: '9px 16px',
        borderRadius: 'var(--radius-pill)',
        fontFamily: 'var(--font-display)',
        fontSize: '13px',
        fontWeight: 'var(--fw-semibold)',
        lineHeight: 1,
        ...tones[tone],
        ...style,
      }}
      {...rest}
    >
      {children}
    </span>
  );
}
