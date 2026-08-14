import React from 'react';

export interface EyebrowProps {
  /** Label text */
  children: React.ReactNode;
  /** Optional numeric prefix, e.g. "01" */
  number?: string;
  /** Override color. @default terracotta */
  color?: string;
  style?: React.CSSProperties;
}

/**
 * Small uppercase kicker above a section title or card. Terracotta by default;
 * use sage for muted in-card labels. Optional numbered prefix ("01 — Философия").
 */
export function Eyebrow(props: EyebrowProps): JSX.Element;
