import React from 'react';
import {Composition, registerRoot} from 'remotion';
import {MiniReel} from './MiniReel';

// Рендер: npx remotion render src/entry-mini.ts Mini out.mp4 --props=...
const Root: React.FC = () =>
  React.createElement(Composition, {
    id: 'Mini',
    component: MiniReel as React.FC<Record<string, unknown>>,
    durationInFrames: 210,
    fps: 30,
    width: 1080,
    height: 1920,
    defaultProps: {src: 'mr1.mp4', head: '', cta: 'СОХРАНЯЙ', rate: 1, grade: ''},
  });

registerRoot(Root);
