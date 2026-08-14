import React from 'react';

/**
 * Primary call-to-action for lily.space. Fully-rounded pill in terracotta,
 * graphite, or outline. Use terracotta for the single primary action per view.
 *
 * @startingPoint section="Core" subtitle="Pill CTA — terracotta / dark / outline" viewport="700x220"
 */
export interface CTAButtonProps {
  /** Button label */
  children: React.ReactNode;
  /** Fill style. @default 'accent' */
  variant?: 'accent' | 'dark' | 'outline';
  /** Size. @default 'md' */
  size?: 'md' | 'sm';
  /** Append a trailing ↗ arrow. @default false */
  arrow?: boolean;
  /** Render as an anchor when set */
  href?: string;
  style?: React.CSSProperties;
}

/**
 * Primary call-to-action for lily.space.
 */
export function CTAButton(props: CTAButtonProps): JSX.Element;
