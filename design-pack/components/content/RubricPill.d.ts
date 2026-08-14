import React from 'react';

export interface RubricPillProps {
  /** Rubric label */
  children: React.ReactNode;
  /** Tone. @default 'dark' */
  tone?: 'dark' | 'outline' | 'sage' | 'terracotta';
  style?: React.CSSProperties;
}

/**
 * Rounded content-rubric tag (e.g. "ИИ для бизнеса", "Промпты"). Use the dark
 * tone for the active/primary rubric and outline for the rest; sage and
 * terracotta tints flag special categories.
 */
export function RubricPill(props: RubricPillProps): JSX.Element;
