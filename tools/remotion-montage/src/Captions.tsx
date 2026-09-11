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

// Где стоит строка и чем отбита от кадра. Дефолты — поведение `Reel`,
// каким оно было до сплита: низ кадра и мягкая подложка градиентом.
export type CaptionStyle = {
  // Верх строки в пикселях от верха холста. Задан — строка становится
  // якорной сверху: в сплите она садится ровно на шов, а не «примерно
  // там», и шов держит её при любой высоте панели.
  top?: number;
  fontSize?: number;
  uppercase?: boolean;
  // Плотная чёрная плашка по тексту вместо градиента во весь кадр.
  // На шве градиент не работает: он затемняет низ панели, и склейка
  // двух картинок превращается в грязное пятно.
  plate?: boolean;
  // Строка висит **над** линией `top`, а не под ней. Нужно на сплите:
  // под швом начинается дубль, и в вертикальном кадре лицо стоит ровно
  // там — слова ложились человеку на лицо. Над швом у панели пустое
  // поле, и строка живёт на нём, ничего не закрывая.
  above?: boolean;
};

const CaptionPage: React.FC<{
  page: Page;
  accent: string;
  look: CaptionStyle;
}> = ({page, accent, look}) => {
  const frame = useCurrentFrame();
  const {fps, height} = useVideoConfig();
  // Время внутри страницы — своё, от начала её Sequence. Абсолютное
  // получаем сложением, иначе подсветка уедет на второй же странице.
  const now = page.start + frame / fps;
  const anchored = look.top !== undefined;

  const above = anchored && look.above;

  return (
    <AbsoluteFill
      style={{
        justifyContent: anchored && !above ? 'flex-start' : 'flex-end',
        alignItems: 'center',
        paddingTop: anchored && !above ? look.top : undefined,
        paddingBottom: above
          ? height - (look.top ?? 0)
          : anchored
            ? undefined
            : height * 0.16,
      }}
    >
      {/* Тень под словом спасает на тёмном кадре и не спасает на светлом:
          записи экрана в основном белые, и белый текст по белому документу
          читается через раз. Мягкая подложка снизу стоит дешевле, чем
          плашка, и не превращает караоке обратно в титр. */}
      {look.plate ? null : (
        <AbsoluteFill
          style={{
            background:
              'linear-gradient(180deg, rgba(0,0,0,0) 55%, rgba(0,0,0,0.55) 78%, rgba(0,0,0,0.7) 100%)',
          }}
        />
      )}
      <div
        style={{
          margin: '0 7%',
          textAlign: 'center',
          fontFamily: FONT,
          fontWeight: 800,
          fontSize: look.fontSize ?? 68,
          lineHeight: 1.15,
          color: '#fff',
          whiteSpace: 'pre-wrap',
          textTransform: look.uppercase ? 'uppercase' : undefined,
          ...(look.plate
            ? {
                background: '#000',
                padding: '0.10em 0.28em',
                boxDecorationBreak: 'clone',
              }
            : {
                textShadow:
                  '0 4px 24px rgba(0,0,0,0.85), 0 1px 3px rgba(0,0,0,0.9)',
                WebkitTextStroke: '2px rgba(0,0,0,0.45)',
                paintOrder: 'stroke fill',
              }),
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
  look?: CaptionStyle;
}> = ({pages, accent, look = {}}) => {
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
            <CaptionPage page={page} accent={accent} look={look} />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
