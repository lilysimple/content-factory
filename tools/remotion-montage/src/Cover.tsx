import React from 'react';
import {AbsoluteFill, Easing, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {loadFont as loadManrope} from '@remotion/google-fonts/Manrope';
import {loadFont as loadMontserrat} from '@remotion/google-fonts/Montserrat';
import {loadFont as loadUnbounded} from '@remotion/google-fonts/Unbounded';
import {loadFont as loadGolos} from '@remotion/google-fonts/GolosText';
import type {ReelProps} from './props';

// Шрифт обложки называется в ТЗ бренда, а не зашит здесь: сменить его —
// это правка одной строки в `design/platforms/…-cover.md`, а не в коде.
//
// Грузим через @remotion/google-fonts, а не CSS-импортом: пакет блокирует
// рендер до готовности шрифта, а импорт — нет, и кадр успевает сняться
// системным. На этом уже обжигались в макетах (`design.py`,
// --virtual-time-budget).
//
// Импорты статические и все сразу: сборщик Remotion должен видеть их до
// рендера, динамический import по имени из props он не разрешит.
const SUBSETS = ['cyrillic', 'latin'] as const;

const FONTS: Record<string, string> = {
  Manrope: loadManrope('normal', {
    weights: ['500', '800'],
    subsets: [...SUBSETS],
  }).fontFamily,
  Montserrat: loadMontserrat('normal', {
    weights: ['500', '900'],
    subsets: [...SUBSETS],
  }).fontFamily,
  Unbounded: loadUnbounded('normal', {
    weights: ['500', '900'],
    subsets: [...SUBSETS],
  }).fontFamily,
  GolosText: loadGolos('normal', {
    weights: ['500', '900'],
    subsets: [...SUBSETS],
  }).fontFamily,
};

// Текст обложки набирает питон: разбивку на строки, цвет каждой строки и
// кегль он считает по ТЗ бренда (`design/platforms/{площадка}-{формат}-cover.md`).
// Здесь только рисование — так правило «строка тянется на всю ширину»
// живёт в одном месте и проверяется стендом без браузера.
export const Cover: React.FC<{
  lines: ReelProps['coverLines'];
  title?: string;
  titleColor: string;
  brandName?: string;
  font: string;
  weight: number;
  titleSize: number;
  // Куда лёг блок и на сколько отступил от своего края. Считает питон по
  // рамке лица (`montage.place_cover`): на портретном кадре низ занят
  // лицом, и текст уходит наверх. Здесь только рисование.
  anchor: 'top' | 'bottom';
  inset: number;
}> = ({
  lines,
  title,
  titleColor,
  brandName,
  font,
  weight,
  titleSize,
  anchor,
  inset,
}) => {
  const fontFamily = FONTS[font] ?? FONTS.Manrope;
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  return (
    <AbsoluteFill
      style={{
        justifyContent: anchor === 'top' ? 'flex-start' : 'flex-end',
        alignItems: 'center',
        padding:
          anchor === 'top' ? `${inset}px 7% 0` : `0 7% ${inset}px`,
        fontFamily,
      }}
    >
      <div style={{width: '100%', textAlign: 'center'}}>
        {title ? (
          <div
            style={{
              opacity: interpolate(frame, [0, fps * 0.35], [0, 1], {
                extrapolateLeft: 'clamp',
                extrapolateRight: 'clamp',
                easing: Easing.bezier(0.16, 1, 0.3, 1),
              }),
              color: titleColor,
              fontWeight: 500,
              fontSize: titleSize,
              lineHeight: 1.25,
              marginBottom: 18,
              textShadow: '0 2px 16px rgba(0,0,0,0.6)',
            }}
          >
            {title}
          </div>
        ) : null}

        {lines.map((line, i) => (
          <div
            key={`${line.text}-${i}`}
            style={{
              // Строки въезжают по очереди, а не разом: обложка читается
              // сверху вниз, и глазу так легче поймать порядок.
              opacity: interpolate(
                frame,
                [i * fps * 0.08, i * fps * 0.08 + fps * 0.3],
                [0, 1],
                {
                  extrapolateLeft: 'clamp',
                  extrapolateRight: 'clamp',
                  easing: Easing.bezier(0.16, 1, 0.3, 1),
                },
              ),
              translate:
                interpolate(
                  frame,
                  [i * fps * 0.08, i * fps * 0.08 + fps * 0.45],
                  [22, 0],
                  {
                    extrapolateLeft: 'clamp',
                    extrapolateRight: 'clamp',
                    easing: Easing.bezier(0.16, 1, 0.3, 1),
                  },
                ).toFixed(2) + 'px',
              color: line.color,
              fontWeight: weight,
              fontSize: line.size,
              lineHeight: 1.04,
              letterSpacing: '-0.035em',
              textShadow: '0 6px 30px rgba(0,0,0,0.45)',
            }}
          >
            {line.text}
          </div>
        ))}

        {brandName ? (
          <div
            style={{
              marginTop: 22,
              color: '#fff',
              opacity: 0.8,
              fontWeight: 500,
              fontSize: 30,
            }}
          >
            {brandName}
          </div>
        ) : null}
      </div>
    </AbsoluteFill>
  );
};
