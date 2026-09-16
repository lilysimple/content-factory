import React from 'react';
import {
  AbsoluteFill,
  Easing,
  Img,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';

const FONT = 'system-ui, -apple-system, Helvetica, sans-serif';
const EASE = Easing.bezier(0.16, 1, 0.3, 1);

// Обложка сплита по референсу: полноразмерный кадр дубля с лицом, сверху
// белая плашка со знаком и именем продукта, под ней заголовок на белых
// плашках. Слова из `accent` — цветом бренда. В сетке профиля ролик
// узнаётся по человеку и знаку, а панели там не видно вовсе.
export const SplitCover: React.FC<{
  path: string;
  focus: {x: number; y: number};
  lines: string[];
  accent: string[];
  accentColor: string;
  logoPath?: string;
  name?: string;
}> = ({path, focus, lines, accent, accentColor, logoPath, name}) => {
  const frame = useCurrentFrame();
  const {fps, width} = useVideoConfig();
  const pop = spring({frame, fps, config: {damping: 15, mass: 0.6}});
  const marks = new Set(accent.map((w) => w.toLowerCase()));
  const plain = (w: string) => w.toLowerCase().replace(/[.,!?:;«»"()]/g, '');
  const size = width * 0.066;

  return (
    <AbsoluteFill>
      <Img
        src={staticFile(path)}
        style={{
          width: '100%',
          height: '100%',
          objectFit: 'cover',
          objectPosition: `${(focus.x * 100).toFixed(1)}% ${(focus.y * 100).toFixed(1)}%`,
          scale: String(interpolate(frame, [0, fps * 1.5], [1.06, 1], {
            extrapolateRight: 'clamp',
          })),
        }}
      />
      <AbsoluteFill
        style={{
          justifyContent: 'flex-start',
          alignItems: 'center',
          paddingTop: width * 0.1,
          gap: width * 0.022,
        }}
      >
        {logoPath || name ? (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: width * 0.02,
              background: '#fff',
              borderRadius: width * 0.03,
              padding: `${width * 0.018}px ${width * 0.04}px`,
              boxShadow: '0 10px 40px rgba(0,0,0,0.25)',
              marginBottom: width * 0.01,
              scale: String(interpolate(pop, [0, 1], [0.8, 1])),
              opacity: interpolate(pop, [0, 0.5], [0, 1], {
                extrapolateRight: 'clamp',
              }),
            }}
          >
            {logoPath ? (
              <Img
                src={staticFile(logoPath)}
                style={{width: width * 0.1, height: width * 0.1, objectFit: 'contain'}}
              />
            ) : null}
            {name ? (
              <div
                style={{
                  fontFamily: 'Georgia, "Times New Roman", serif',
                  fontSize: width * 0.1,
                  lineHeight: 1,
                  color: '#1a1a1a',
                  letterSpacing: '-0.02em',
                }}
              >
                {name}
              </div>
            ) : null}
          </div>
        ) : null}
        {lines.map((line, i) => {
          const at = fps * (0.12 + i * 0.08);
          const show = interpolate(frame, [at, at + fps * 0.3], [0, 1], {
            extrapolateLeft: 'clamp',
            extrapolateRight: 'clamp',
            easing: EASE,
          });
          return (
            <div
              key={`${line}-${i}`}
              style={{
                background: '#fff',
                borderRadius: width * 0.022,
                padding: `${size * 0.14}px ${size * 0.4}px`,
                fontFamily: FONT,
                fontWeight: 800,
                fontSize: size,
                lineHeight: 1.12,
                color: '#111',
                textAlign: 'center',
                maxWidth: width * 0.9,
                boxShadow: '0 8px 30px rgba(0,0,0,0.2)',
                opacity: show,
                translate: `0 ${interpolate(show, [0, 1], [size * 0.4, 0])}px`,
              }}
            >
              {line.split(' ').map((w, j) => (
                <span key={j} style={{color: marks.has(plain(w)) ? accentColor : '#111'}}>
                  {j ? ' ' : ''}
                  {w}
                </span>
              ))}
            </div>
          );
        })}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
