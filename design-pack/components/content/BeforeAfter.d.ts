import React from 'react';

export interface BeforeAfterProps {
  /** Left tile label. @default 'Без AI' */
  beforeLabel?: React.ReactNode;
  /** Left tile value */
  beforeValue: React.ReactNode;
  /** Right tile label. @default 'С AI' */
  afterLabel?: React.ReactNode;
  /** Right tile value */
  afterValue: React.ReactNode;
  style?: React.CSSProperties;
}

/**
 * "До / после" contrast — a neutral tile and a graphite tile split by a
 * terracotta arrow. Use to dramatise a before/after transformation.
 */
export function BeforeAfter(props: BeforeAfterProps): JSX.Element;
