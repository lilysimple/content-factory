import React from 'react';

/**
 * Dark metric / infographic card — a large terracotta-accented number with an
 * optional progress bar. Use for a single hero statistic in a carousel or post.
 *
 * @startingPoint section="Content" subtitle="Dark metric card with progress bar" viewport="380x300"
 */
export interface StatCardProps {
  /** Headline figure, e.g. "−40%" */
  value: React.ReactNode;
  /** Supporting sentence under the figure */
  label?: React.ReactNode;
  /** Small uppercase terracotta kicker */
  eyebrow?: React.ReactNode;
  /** Progress-bar fill, 0–100 */
  progress?: number;
  /** Left range label under the bar */
  from?: React.ReactNode;
  /** Right range label under the bar */
  to?: React.ReactNode;
  style?: React.CSSProperties;
}

/** Dark metric / infographic card. */
export function StatCard(props: StatCardProps): JSX.Element;
