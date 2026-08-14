import React from 'react';

/**
 * CTAButton — lily.space call-to-action / button.
 * Fully-rounded pill. Three fills: terracotta accent, graphite, and outline.
 */
export function CTAButton({
  children,
  variant = 'accent',   // 'accent' | 'dark' | 'outline'
  size = 'md',          // 'md' | 'sm'
  arrow = false,        // append a trailing ↗ arrow
  href,
  style = {},
  ...rest
}) {
  const sizes = {
    md: { padding: '13px 24px', fontSize: '15px' },
    sm: { padding: '9px 18px', fontSize: '13px' },
  };
  const variants = {
    accent:  { background: 'var(--accent)', color: '#fff', border: '1.5px solid transparent' },
    dark:    { background: 'var(--surface-inverse)', color: 'var(--text-on-dark)', border: '1.5px solid transparent' },
    outline: { background: 'transparent', color: 'var(--text-primary)', border: '1.5px solid rgba(31,31,31,0.18)' },
  };

  const base = {
    display: 'inline-flex',
    alignItems: 'center',
    gap: '8px',
    fontFamily: 'var(--font-display)',
    fontWeight: 'var(--fw-semibold)',
    borderRadius: 'var(--radius-pill)',
    textDecoration: 'none',
    cursor: 'pointer',
    lineHeight: 1,
    whiteSpace: 'nowrap',
    transition: 'filter var(--dur-fast) var(--ease-smooth), transform var(--dur-fast) var(--ease-smooth)',
    ...sizes[size],
    ...variants[variant],
    ...style,
  };

  const Tag = href ? 'a' : 'button';
  return (
    <Tag href={href} style={base} {...rest}>
      {children}
      {arrow && <span aria-hidden="true">↗</span>}
    </Tag>
  );
}
