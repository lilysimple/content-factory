import React from 'react';

/**
 * Soft sage-tinted card for a prompt template or pull-quote, with an optional
 * author row. Use for shareable prompt snippets and short quotes.
 *
 * @startingPoint section="Content" subtitle="Sage prompt / quote card" viewport="380x260"
 */
export interface QuoteCardProps {
  /** Quote or prompt text */
  children: React.ReactNode;
  /** Uppercase kicker. @default 'Промпт' */
  eyebrow?: React.ReactNode;
  /** Author label shown under the quote */
  author?: React.ReactNode;
  /** Avatar image src next to the author */
  avatar?: string;
  style?: React.CSSProperties;
}

/** Soft sage-tinted prompt / quote card. */
export function QuoteCard(props: QuoteCardProps): JSX.Element;
