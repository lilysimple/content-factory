import React from 'react';
import {AbsoluteFill, Sequence, useCurrentFrame, useVideoConfig} from 'remotion';
import type {ReelProps} from './props';

// Караоке живёт отдельным файлом, как советует сам Remotion
// (remotion-best-practices, remotion-captions/display-captions.md):
// подсветка активного слова — единственное, что тут считается на каждом
// кадре, и держать это рядом со сборкой ролика значит пересчитывать её
// вместе со всем остальным.

const FONT = 'system-ui, -apple-system, Helvetica, sans-serif';

type Page = ReelProps['pages'][number];

const CaptionPage: React.FC<{page: Page; accent: string}> = ({page, accent}) => {
  const frame = useCurrentFrame();
  const {fps, height} = useVideoConfig();
  // Время внутри страницы — своё, от начала её Sequence. Абсолютное
  // получаем сложением, иначе подсветка уедет на второй же странице.
  const now = page.start + frame / fps;

  return (
    <AbsoluteFill
      style={{
        justifyContent: 'flex-end',
        alignItems: 'center',
        paddingBottom: height * 0.16,
      }}
    >
      {/* Тень под словом спасает на тёмном кадре и не спасает на светлом:
          записи экрана в основном белые, и белый текст по белому документу
          читается через раз. Мягкая подложка снизу стоит дешевле, чем
          плашка, и не превращает караоке обратно в титр. */}
      <AbsoluteFill
        style={{
          background:
            'linear-gradient(180deg, rgba(0,0,0,0) 55%, rgba(0,0,0,0.55) 78%, rgba(0,0,0,0.7) 100%)',
        }}
      />
      <div
        style={{
          margin: '0 7%',
          textAlign: 'center',
          fontFamily: FONT,
          fontWeight: 800,
          fontSize: 68,
          lineHeight: 1.15,
          color: '#fff',
          whiteSpace: 'pre-wrap',
          textShadow: '0 4px 24px rgba(0,0,0,0.85), 0 1px 3px rgba(0,0,0,0.9)',
          WebkitTextStroke: '2px rgba(0,0,0,0.45)',
          paintOrder: 'stroke fill',
        }}
      >
        {page.words.map((w, i) => (
          <span
            key={`${w.start}-${i}`}
            style={{
              display: 'inline-block',
              margin: '0 0.14em',
              color: now >= w.start && now < w.end ? accent : '#fff',
              scale: now >= w.start && now < w.end ? 1.08 : 1,
            }}
          >
            {w.text}
          </span>
        ))}
      </div>
    </AbsoluteFill>
  );
};

// Страницы приходят готовым списком из питона, поэтому здесь `.map()`, а
// не набор рукописных клипов, как советует Remotion для монтажа в Studio:
// эту дорожку никто не двигает руками, её пересобирает следующий прогон.
export const Captions: React.FC<{
  pages: ReelProps['pages'];
  accent: string;
}> = ({pages, accent}) => {
  const {fps} = useVideoConfig();

  return (
    <AbsoluteFill>
      {pages.map((page, i) => {
        const from = Math.round(page.start * fps);
        const next = pages[i + 1];
        // Страница висит до следующей, но не дольше собственного хвоста:
        // после последнего слова текст на экране мешает смотреть.
        const until = Math.min(
          next ? Math.round(next.start * fps) : Infinity,
          Math.round((page.end + 0.35) * fps),
        );
        const durationInFrames = until - from;
        if (durationInFrames <= 0) {
          return null;
        }
        return (
          <Sequence
            key={`${page.start}-${i}`}
            from={from}
            durationInFrames={durationInFrames}
            name={`Субтитр ${i + 1}`}
          >
            <CaptionPage page={page} accent={accent} />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
