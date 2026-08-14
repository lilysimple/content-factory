import React from 'react';

export interface NumberedStep {
  /** Step heading */
  title: React.ReactNode;
  /** Optional supporting note under the heading */
  note?: React.ReactNode;
}

/**
 * Ordered-steps card with terracotta numerals and hairline dividers. Use for
 * short how-to sequences (3–4 steps ideal).
 *
 * @startingPoint section="Content" subtitle="Numbered steps card" viewport="380x300"
 */
export interface NumberedListProps {
  /** Ordered steps */
  items: NumberedStep[];
  /** Optional uppercase sage kicker */
  eyebrow?: React.ReactNode;
  style?: React.CSSProperties;
}

/** Ordered-steps card with terracotta numerals. */
export function NumberedList(props: NumberedListProps): JSX.Element;
