import React, {useLayoutEffect, useRef, useState} from 'react';
import {Video} from '@remotion/media';
import {
  AbsoluteFill,
  Easing,
  continueRender,
  delayRender,
  Freeze,
  Img,
  Sequence,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';
import type {MotionBlock, MotionProps} from './motionProps';

const FONT = 'system-ui, -apple-system, Helvetica, sans-serif';

// Панель сплита: половина кадра, на которой идёт инфографика.
//
// Пропорции здесь считаются от высоты самой панели, а не от высоты
// холста, и это принципиально: блок `full` разворачивает ту же панель на
// весь кадр, и кегль, привязанный к холсту, там бы не поехал, а прыгнул.

const EASE = Easing.bezier(0.16, 1, 0.3, 1);

// Список набирается под речь: пункт выезжает в ту секунду, когда человек
// его называет, а не вместе со всей карточкой. Список, показанный стеной,
// читается быстрее речи — зритель дочитал и ушёл раньше, чем автор дошёл
// до второго пункта.
const Items: React.FC<{
  items: MotionBlock['items'];
  start: number;
  accentColor: string;
  textColor: string;
  panelHeight: number;
}> = ({items, start, accentColor, textColor, panelHeight}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  return (
    <div style={{display: 'flex', flexDirection: 'column', width: '100%'}}>
      {items.map((item, i) => {
        // Секунды пункта — секунды готового ролика, а отсчёт внутри
        // блока идёт от его начала. Полный кадр приходит уже сдвинутым.
        const at = Math.max(0, (item.at - start) * fps);
        const show = interpolate(frame, [at, at + fps * 0.22], [0, 1], {
          extrapolateLeft: 'clamp',
          extrapolateRight: 'clamp',
          easing: EASE,
        });
        return (
          <div
            key={`${item.text}-${i}`}
            style={{
              display: 'flex',
              alignItems: 'baseline',
              gap: panelHeight * 0.025,
              marginTop: i === 0 ? 0 : panelHeight * 0.028,
              fontFamily: FONT,
              fontSize: panelHeight * 0.056,
              fontWeight: 600,
              lineHeight: 1.25,
              color: textColor,
              opacity: show,
              // Пункт приезжает слева, как строка списка, а не
              // проявляется на месте: проявление читается как подмена
              // предыдущего пункта.
              translate: `${interpolate(show, [0, 1], [-panelHeight * 0.03, 0])}px 0`,
            }}
          >
            <span style={{color: accentColor, fontWeight: 700}}>—</span>
            <span>{item.text}</span>
          </div>
        );
      })}
    </div>
  );
};

// Иконка продукта и его имя под ней — приём референса: на панели стоит
// не скрин интерфейса, а знак, который читается за полкадра.
const IconCard: React.FC<{
  block: MotionBlock;
  cardColor: string;
  textColor: string;
  panelHeight: number;
}> = ({block, cardColor, textColor, panelHeight}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  // Пружина, а не линейный вход: знак прилетает с перелётом, как штамп.
  const pop = spring({frame, fps, config: {damping: 13, mass: 0.5}});
  // С надзаголовком плитка мельче: иначе плитка с именем под ней не
  // помещается между надзаголовком и полосой караоке и наезжает на него.
  const size = panelHeight * (block.imagePath && block.kicker ? 0.27 : 0.38);

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: panelHeight * 0.045,
      }}
    >
      {block.imagePath ? (
        <div
          style={{
            width: size,
            height: size,
            // Плитка белая, а не подложка панели: знаки рисуют под
            // светлый фон, и тёмный логотип на графите пропадает. Так же
            // сделано в референсе — знак в белой плитке, как иконка
            // приложения.
            borderRadius: size * 0.24,
            background: '#FFFFFF',
            boxShadow: `0 ${size * 0.06}px ${size * 0.2}px rgba(0,0,0,0.35)`,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            overflow: 'hidden',
            scale: interpolate(pop, [0, 1], [0.7, 1], {
              output: 'perceptual-scale',
            }),
            // Плитка не стоит мёртвой: еле заметно качается, пока блок
            // на экране. Неподвижная картинка читается скрином, а не
            // движением.
            translate: `0 ${Math.sin(frame / fps * 1.6) * size * 0.015}px`,
          }}
        >
          <Img
            src={staticFile(block.imagePath)}
            style={{width: '68%', height: '68%', objectFit: 'contain'}}
          />
        </div>
      ) : null}
      {block.lines.map((line, i) => (
        <div
          key={`${line}-${i}`}
          style={{
            fontFamily: FONT,
            color: textColor,
            fontSize: i === 0 ? panelHeight * 0.105 : panelHeight * 0.058,
            fontWeight: i === 0 ? 700 : 500,
            opacity: i === 0 ? interpolate(pop, [0.3, 1], [0, 1], {
              extrapolateLeft: 'clamp',
              extrapolateRight: 'clamp',
            }) : 0.75,
            letterSpacing: i === 0 ? '-0.02em' : undefined,
          }}
        >
          {line}
        </div>
      ))}
    </div>
  );
};

// Число набирается от нуля ровно в ту секунду, когда человек его
// произносит. Готовое число на экране — картинка; набирающееся —
// движение, и стоит оно двух строк кода.
const Counter: React.FC<{
  block: MotionBlock;
  accentColor: string;
  textColor: string;
  panelHeight: number;
}> = ({block, accentColor, textColor, panelHeight}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const at = Math.max(0, (block.valueAt ?? block.start) - block.start);
  const target = block.value ?? 0;
  const shown = Math.round(
    interpolate(frame, [at * fps, (at + 0.7) * fps], [0, target], {
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
      easing: EASE,
    }),
  );

  return (
    <div style={{display: 'flex', flexDirection: 'column', alignItems: 'center'}}>
      <div
        style={{
          fontFamily: FONT,
          fontWeight: 800,
          fontSize: panelHeight * 0.42,
          lineHeight: 1,
          color: accentColor,
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        {shown}
      </div>
      {block.lines.map((line, i) => (
        <div
          key={`${line}-${i}`}
          style={{
            fontFamily: FONT,
            color: textColor,
            fontSize: i === 0 ? panelHeight * 0.075 : panelHeight * 0.055,
            fontWeight: i === 0 ? 700 : 500,
            opacity: i === 0 ? 1 : 0.78,
            marginTop: panelHeight * 0.03,
          }}
        >
          {line}
        </div>
      ))}
    </div>
  );
};

// Шкала: ступени стоят на дорожке, бегунок едет по ним под речь.
// Подпись ступени — `low — быстро`: слева имя ступени, справа пояснение,
// и пояснение показывается только у той, на которой бегунок сейчас.
const Scale: React.FC<{
  block: MotionBlock;
  accentColor: string;
  textColor: string;
  panelHeight: number;
}> = ({block, accentColor, textColor, panelHeight}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const stops = block.items;
  if (stops.length === 0) {
    return null;
  }

  const now = frame / fps;
  const span = Math.max(0.001, block.end - block.start);
  const raw = stops.map((s) => Math.max(0, s.at - block.start));
  // Ступени без своих слов приезжают все на начало блока — питон так и
  // задумал, — а `interpolate` требует строго растущий ряд и падает на
  // одинаковых. Ряд не вырос — раскладываем ступени ровно по блоку:
  // шкала, которая не поехала, полезнее рендера, который не собрался.
  const rising = raw.every((t, i) => i === 0 || t > raw[i - 1]);
  const times = rising
    ? raw
    : stops.map((_, i) => (span * (i + 1)) / (stops.length + 1));
  const idx = times.map((_, i) => i);
  // Бегунок едет между ступенями, а не прыгает: позиция считается
  // интерполяцией по тем же секундам, что и пункты списка.
  const pos =
    stops.length === 1
      ? 0
      : interpolate(now, times, idx, {
          extrapolateLeft: 'clamp',
          extrapolateRight: 'clamp',
          // Не `EASE`: он вылетает к следующей ступени за первую треть
          // отрезка, и бегунок стоял на «high», пока человек говорил про
          // «normal». Шкала должна идти ровно со словами.
          easing: Easing.inOut(Easing.ease),
        });
  const active = Math.round(pos);
  const width = panelHeight * 1.05;
  const step = stops.length > 1 ? width / (stops.length - 1) : 0;
  const dot = panelHeight * 0.055;
  const name = (text: string) => text.split(/\s+—\s+/)[0];
  const note = (text: string) => text.split(/\s+—\s+/)[1] ?? '';

  return (
    <div style={{display: 'flex', flexDirection: 'column', alignItems: 'center'}}>
      <div style={{position: 'relative', width, height: dot * 2}}>
        {/* дорожка целиком и её пройденная часть */}
        <div
          style={{
            position: 'absolute',
            top: dot * 0.85,
            left: 0,
            width,
            height: dot * 0.3,
            borderRadius: dot,
            background: textColor,
            opacity: 0.18,
          }}
        />
        <div
          style={{
            position: 'absolute',
            top: dot * 0.85,
            left: 0,
            width: step * pos,
            height: dot * 0.3,
            borderRadius: dot,
            background: accentColor,
          }}
        />
        {stops.map((s, i) => (
          <div
            key={`${s.text}-${i}`}
            style={{
              position: 'absolute',
              top: dot * 0.5,
              left: step * i - dot * 0.5,
              width: dot,
              height: dot,
              borderRadius: dot,
              background: i <= pos + 0.001 ? accentColor : textColor,
              opacity: i <= pos + 0.001 ? 1 : 0.25,
            }}
          />
        ))}
        {/* бегунок */}
        <div
          style={{
            position: 'absolute',
            top: 0,
            left: step * pos - dot,
            width: dot * 2,
            height: dot * 2,
            borderRadius: dot * 2,
            border: `${dot * 0.28}px solid ${accentColor}`,
            background: 'rgba(0,0,0,0.35)',
          }}
        />
      </div>

      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          width: width + dot * 2,
          marginTop: panelHeight * 0.03,
        }}
      >
        {stops.map((s, i) => (
          <div
            key={`name-${i}`}
            style={{
              fontFamily: FONT,
              fontSize: panelHeight * 0.052,
              fontWeight: 700,
              color: i === active ? accentColor : textColor,
              opacity: i === active ? 1 : 0.4,
              textAlign: 'center',
              flex: 1,
            }}
          >
            {name(s.text)}
          </div>
        ))}
      </div>

      <div
        style={{
          fontFamily: FONT,
          fontSize: panelHeight * 0.062,
          fontWeight: 600,
          color: textColor,
          marginTop: panelHeight * 0.04,
          minHeight: panelHeight * 0.08,
        }}
      >
        {note(stops[active]?.text ?? '')}
      </div>
    </div>
  );
};

// Скрин страницы продукта — окном браузера, как в референсе: полоса с
// тремя точками сверху, под ней страница. Окно едет, а не стоит: за время
// блока оно медленно наезжает и прокручивает страницу вниз. Скрин,
// который стоит на месте три секунды, читается слайдом презентации.
const Window: React.FC<{
  block: MotionBlock;
  cardColor: string;
  accentColor: string;
  textColor: string;
  panelHeight: number;
  maxHeight: number;
}> = ({block, accentColor, textColor, panelHeight, maxHeight}) => {
  const frame = useCurrentFrame();
  const {fps, width} = useVideoConfig();
  const life = Math.max(1, (block.end - block.start) * fps);
  const t = interpolate(frame, [0, life], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  // Окно въезжает снизу с пружиной — тот же характер, что у знака.
  const rise = spring({frame, fps, config: {damping: 16, mass: 0.6}});
  const bar = panelHeight * 0.045;
  const radius = panelHeight * 0.03;
  const caption = block.lines[0];
  // Окно не во всю отведённую высоту: оно ещё наезжает и въезжает снизу,
  // и впритык к краю панели верх страницы срезало швом.
  const winHeight = panelHeight * (block.kicker ? 0.4 : 0.58)
    - (caption ? panelHeight * 0.1 : 0);
  const winWidth = Math.min(width * 0.8, winHeight * 1.45);

  return (
    <div style={{display: 'flex', flexDirection: 'column', alignItems: 'center'}}>
      <div
        style={{
          width: winWidth,
          height: winHeight,
          borderRadius: radius,
          overflow: 'hidden',
          background: '#FFFFFF',
          boxShadow: `0 ${panelHeight * 0.03}px ${panelHeight * 0.09}px rgba(0,0,0,0.45)`,
          outline: `${Math.max(2, panelHeight * 0.004)}px solid ${accentColor}`,
          display: 'flex',
          flexDirection: 'column',
          opacity: interpolate(rise, [0, 0.4], [0, 1], {
            extrapolateLeft: 'clamp',
            extrapolateRight: 'clamp',
          }),
          translate: `0 ${interpolate(rise, [0, 1], [panelHeight * 0.12, 0])}px`,
          // Наезд на всё время блока, едва заметный: 4 процента.
          scale: interpolate(t, [0, 1], [1, 1.04]),
        }}
      >
        <div
          style={{
            height: bar,
            flexShrink: 0,
            background: '#EDEBE8',
            display: 'flex',
            alignItems: 'center',
            gap: bar * 0.3,
            paddingLeft: bar * 0.5,
          }}
        >
          {['#FF5F57', '#FEBC2E', '#28C840'].map((c) => (
            <div
              key={c}
              style={{width: bar * 0.32, height: bar * 0.32, borderRadius: bar, background: c}}
            />
          ))}
        </div>
        <div style={{flex: 1, overflow: 'hidden', position: 'relative'}}>
          <Img
            src={staticFile(block.imagePath!)}
            style={{
              width: '100%',
              height: '100%',
              objectFit: 'cover',
              // Прокрутка: первая треть блока — верх страницы, где имя
              // продукта, дальше страница медленно уходит вниз.
              objectPosition: `50% ${interpolate(t, [0.3, 1], [0, 35], {
                extrapolateLeft: 'clamp',
                extrapolateRight: 'clamp',
                easing: Easing.inOut(Easing.ease),
              })}%`,
            }}
          />
        </div>
      </div>
      {caption ? (
        <div
          style={{
            marginTop: panelHeight * 0.035,
            fontFamily: FONT,
            fontWeight: 700,
            fontSize: panelHeight * 0.058,
            color: textColor,
            textAlign: 'center',
            whiteSpace: 'nowrap',
          }}
        >
          {caption}
        </div>
      ) : null}
    </div>
  );
};

// Размытая копия скрина за окном — подложка на всю панель. В референсе
// окно стоит не на пустом фоне, а на своём же расфокусе: панель берёт
// цвет продукта, и склейка со следующим блоком не выглядит дырой.
const Backdrop: React.FC<{src: string; panelColor?: string}> = ({src}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  return (
    <AbsoluteFill style={{overflow: 'hidden'}}>
      <Img
        src={staticFile(src)}
        style={{
          width: '100%',
          height: '100%',
          objectFit: 'cover',
          filter: 'blur(40px) saturate(1.2)',
          opacity: 0.2,
          scale: String(1.25 + (frame / fps) * 0.01),
        }}
      />
    </AbsoluteFill>
  );
};

// Материал из альбома: запись экрана или картинка, которую автор снял
// сам. В референсе это главное на панели — промпт печатается в окне, пока
// автор про него говорит. Кадр вписывается целиком со скруглением, без
// рамки браузера: это не сайт, а экран автора.
const Screen: React.FC<{
  block: MotionBlock;
  textColor: string;
  panelHeight: number;
  maxHeight: number;
}> = ({block, textColor, panelHeight, maxHeight}) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const rise = spring({frame, fps, config: {damping: 16, mass: 0.6}});
  const life = Math.max(1, (block.end - block.start) * fps);
  const t = Math.min(1, frame / life);

  const caption = block.lines[0];
  const boxW = width * (block.full ? 0.9 : 0.84);
  const boxH = (block.full ? height * 0.62 : maxHeight)
    - (caption ? panelHeight * 0.1 : 0);
  const mw = block.mediaWidth || 16;
  const mh = block.mediaHeight || 10;
  const k = Math.min(boxW / mw, boxH / mh);
  const w = mw * k;
  const h = mh * k;
  const radius = panelHeight * 0.03;

  // Длиннее блока — ускоряем, но не больше чем вдвое: быстрее интерфейс
  // уже не читается. Короче — последний кадр стоит до конца блока.
  const blockSec = block.end - block.start;
  const len = block.videoSeconds ?? blockSec;
  const rate = Math.min(2, Math.max(1, len / Math.max(0.1, blockSec)));
  const lastFrame = Math.max(0, Math.floor((len / rate) * fps) - 2);

  return (
    <div style={{display: 'flex', flexDirection: 'column', alignItems: 'center'}}>
      <div
        style={{
          width: w,
          height: h,
          borderRadius: radius,
          overflow: 'hidden',
          position: 'relative',
          background: '#000',
          boxShadow: `0 ${panelHeight * 0.03}px ${panelHeight * 0.09}px rgba(0,0,0,0.5)`,
          opacity: interpolate(rise, [0, 0.4], [0, 1], {
            extrapolateLeft: 'clamp',
            extrapolateRight: 'clamp',
          }),
          translate: `0 ${interpolate(rise, [0, 1], [panelHeight * 0.1, 0])}px`,
          scale: interpolate(t, [0, 1], [1, 1.035]),
        }}
      >
        {block.videoPath ? (
          <Freeze frame={lastFrame} active={(f) => f >= lastFrame}>
            <Video
              src={staticFile(block.videoPath)}
              muted
              playbackRate={rate}
              objectFit="cover"
              style={{position: 'absolute', inset: 0, width: '100%', height: '100%'}}
            />
          </Freeze>
        ) : block.imagePath ? (
          <Img
            src={staticFile(block.imagePath)}
            style={{width: '100%', height: '100%', objectFit: 'cover'}}
          />
        ) : null}
      </div>
      {caption ? (
        <div
          style={{
            marginTop: panelHeight * 0.035,
            fontFamily: FONT,
            fontWeight: 700,
            fontSize: panelHeight * 0.058,
            color: textColor,
            textAlign: 'center',
            whiteSpace: 'nowrap',
          }}
        >
          {caption}
        </div>
      ) : null}
    </div>
  );
};

// Картинка по теме, когда записи экрана нет. Нарисованное стоит на месте,
// поэтому движение даёт шаблон, и его здесь несколько слоёв сразу, иначе
// картинка читается слайдом:
//
// - вход: проявляется из расфокуса и наезжает пружиной;
// - предмет парит — дрейф, покачивание и наклон в перспективе, будто его
//   вертят в руках;
// - по картинке проходит блик, раз в несколько секунд;
// - за предметом дышит свечение акцентом бренда;
// - края уходят в цвет панели, чтобы не читался вставленный прямоугольник.
const Art: React.FC<{
  block: MotionBlock;
  panelColor: string;
  textColor: string;
  panelHeight: number;
  accentColor?: string;
}> = ({block, panelColor, textColor, panelHeight, accentColor = '#ffffff'}) => {
  const frame = useCurrentFrame();
  const {fps, height} = useVideoConfig();
  const sec = frame / fps;
  const life = Math.max(0.1, block.end - block.start);
  const t = Math.min(1, sec / life);
  const pop = spring({frame, fps, config: {damping: 16, mass: 0.9}});
  const caption = block.lines[0];

  const blur = interpolate(frame, [0, fps * 0.5], [18, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  // Блик: проходит за 0.9 с, повторяется каждые 3.2 с.
  const sweep = ((sec + 0.6) % 3.2) / 0.9;
  const glow = 0.35 + 0.25 * Math.sin(sec * 2.1);

  return (
    <AbsoluteFill style={{overflow: 'hidden', perspective: panelHeight * 2.4}}>
      <AbsoluteFill
        style={{
          background: `radial-gradient(circle at 50% 48%, ${accentColor} 0%, rgba(0,0,0,0) 45%)`,
          opacity: glow * 0.35,
        }}
      />
      <AbsoluteFill
        style={{
          scale: String(interpolate(pop, [0, 1], [1.3, 1.06]) + t * 0.12),
          translate: `${Math.sin(sec * 0.9) * panelHeight * 0.025}px ${
            Math.sin(sec * 1.3) * panelHeight * 0.03
          }px`,
          rotate: `${Math.sin(sec * 0.6) * 2.2}deg`,
          transform: `rotateY(${Math.sin(sec * 0.8) * 9}deg) rotateX(${
            Math.cos(sec * 0.7) * 6
          }deg)`,
          filter: `blur(${blur}px)`,
        }}
      >
        <Img
          src={staticFile(block.imagePath!)}
          style={{width: '100%', height: '100%', objectFit: 'cover'}}
        />
        {sweep >= 0 && sweep <= 1 ? (
          <AbsoluteFill
            style={{
              background:
                'linear-gradient(105deg, rgba(255,255,255,0) 35%, rgba(255,255,255,0.28) 50%, rgba(255,255,255,0) 65%)',
              translate: `${interpolate(sweep, [0, 1], [-120, 120])}% 0`,
              mixBlendMode: 'screen',
            }}
          />
        ) : null}
      </AbsoluteFill>
      <AbsoluteFill
        style={{
          background: `radial-gradient(ellipse at 50% 45%, rgba(0,0,0,0) 55%, ${panelColor} 100%)`,
        }}
      />
      {caption ? (
        <AbsoluteFill
          style={{
            justifyContent: 'flex-end',
            alignItems: 'center',
            // Подпись картинки держится выше полосы караоке: на 0.2 она
            // стояла впритык к двухстрочной странице субтитра. На весь
            // кадр строка субтитра опускается к низу холста (до 0.9
            // высоты, две строки — от 0.82), и подпись встаёт над ней —
            // от панели считать нельзя, контейнер тут во весь холст.
            paddingBottom: block.full ? height * 0.21 : panelHeight * 0.29,
          }}
        >
          <div
            style={{
              fontFamily: FONT,
              fontWeight: 700,
              fontSize: panelHeight * 0.07,
              color: textColor,
              // Подпись стоит поверх картинки, а картинка пёстрая: на
              // шестерёнках и каркасе дома строка без подложки читалась
              // через раз. Тёмная подложка по тексту, а не полоса.
              background: 'rgba(0,0,0,0.62)',
              padding: `${panelHeight * 0.012}px ${panelHeight * 0.035}px`,
              borderRadius: panelHeight * 0.02,
              opacity: interpolate(pop, [0.4, 1], [0, 1], {
                extrapolateLeft: 'clamp',
                extrapolateRight: 'clamp',
              }),
              translate: `0 ${interpolate(pop, [0.4, 1], [panelHeight * 0.04, 0], {
                extrapolateLeft: 'clamp',
                extrapolateRight: 'clamp',
              })}px`,
              whiteSpace: 'nowrap',
            }}
          >
            {caption}
          </div>
        </AbsoluteFill>
      ) : null}
    </AbsoluteFill>
  );
};

const Card: React.FC<{
  block: MotionBlock;
  cardColor: string;
  accentColor: string;
  textColor: string;
  panelHeight: number;
  // Сколько высоты карточке оставлено. Скрин приходит любой формы, и
  // высокий залезал на надзаголовок: карточка стоит по центру панели, а
  // надзаголовок — на своей замеренной высоте, и спорят они молча.
  maxHeight: number;
}> = ({block, cardColor, accentColor, textColor, panelHeight, maxHeight}) => {
  if (!block.imagePath && block.lines.length === 0 && block.items.length === 0) {
    return null;
  }

  // Скруглению и полям нужен один масштаб, иначе карточка на полном
  // кадре выглядит другой карточкой, а не той же самой крупнее.
  const radius = panelHeight * 0.033;

  if (block.imagePath) {
    return (
      <Window
        block={block}
        cardColor={cardColor}
        accentColor={accentColor}
        textColor={textColor}
        panelHeight={panelHeight}
        maxHeight={maxHeight}
      />
    );
  }

  return (
    <div
      style={{
        width: '82%',
        borderRadius: radius,
        background: cardColor,
        padding: `${panelHeight * 0.075}px ${panelHeight * 0.06}px`,
        boxSizing: 'border-box',
        fontFamily: FONT,
        color: textColor,
        textAlign: 'center',
      }}
    >
      {block.lines.map((line, i) => (
        <div
          key={`${line}-${i}`}
          style={{
            // Первая строка — то, что человек должен унести; остальные
            // её уточняют. Разный вес важнее разного кегля: на тёмной
            // карточке мелкий светлый текст рассыпается.
            fontSize: i === 0 ? panelHeight * 0.088 : panelHeight * 0.062,
            fontWeight: i === 0 ? 700 : 500,
            opacity: i === 0 ? 1 : 0.78,
            lineHeight: 1.25,
            marginTop: i === 0 ? 0 : panelHeight * 0.02,
          }}
        >
          {line}
        </div>
      ))}
      {block.items.length > 0 ? (
        <div
          style={{
            marginTop: block.lines.length ? panelHeight * 0.05 : 0,
            textAlign: 'left',
          }}
        >
          <Items
            items={block.items}
            start={block.start}
            accentColor={accentColor}
            textColor={textColor}
            panelHeight={panelHeight}
          />
        </div>
      ) : null}
    </div>
  );
};

// Текст блока не лезет на полосу караоке. Высоту здесь считать нечем:
// длину строки, перенос и число пунктов знает только вёрстка. Поэтому
// блок меряется готовым, и если выше отведённого — ужимается `zoom`, а не
// `scale`: `zoom` пересчитывает раскладку, и ужатый блок встаёт по
// центру своего места, а не висит сдвинутым. Кадр ждёт замера
// (`delayRender`), иначе первый кадр блока снялся бы ещё неужатым.
//
// Нашёл живой монтаж 15.09: подпись счётчика в две строки и третий пункт
// списка уходили под плашку субтитра.
const FIT_MIN = 0.55;

const Fit: React.FC<{max: number; children: React.ReactNode}> = ({
  max,
  children,
}) => {
  const ref = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState(1);
  const [handle] = useState(() => delayRender('Панель: замер блока'));

  useLayoutEffect(() => {
    const el = ref.current;
    if (el) {
      const natural = el.scrollHeight;
      if (natural > max) {
        setZoom(Math.max(FIT_MIN, max / natural));
      }
    }
    continueRender(handle);
  }, [handle, max]);

  return (
    <div ref={ref} style={{zoom, display: 'flex', justifyContent: 'center', width: '100%'}}>
      {children}
    </div>
  );
};

const BlockView: React.FC<{
  block: MotionBlock;
  panelHeight: number;
  panelColor: string;
  accentColor: string;
  cardColor: string;
  textColor: string;
}> = ({block, panelHeight, panelColor, accentColor, cardColor, textColor}) => {
  const frame = useCurrentFrame();
  const {fps, width} = useVideoConfig();

  // Надзаголовок идёт одной строкой и в разрядку, поэтому кегль ему
  // ставит ширина кадра, а не только высота панели: на полном кадре
  // высота вчетверо больше, и «КОНТЕНТ-ПЛАН» уезжал за оба края
  // обрезанным. Ширина буквы капсом с трекингом 0.22em — примерно
  // 0.85 кегля, и этого хватает: строка всё равно стоит по центру.
  const kickerSize = Math.min(
    panelHeight * 0.062,
    (width * 0.86) / Math.max(1, (block.kicker ?? '').length * 0.85),
  );

  // Блок приезжает за четверть секунды. Дольше — и панель начинает жить
  // своей жизнью, отвлекая от речи; мгновенно — и склейка читается как
  // подмена кадра, а не как следующий пункт того же списка.
  const enter = interpolate(frame, [0, fps * 0.25], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: EASE,
  });

  return (
    <AbsoluteFill
      style={{
        justifyContent: 'center',
        alignItems: 'center',
        opacity: enter,
        // Низ панели отдан караоке: строка садится на шов и на длинной
        // странице растёт вверх — тремя строками она накрывала подпись
        // под счётчиком. Содержимое блока держится выше этой полосы.
        // 0.2, а не 0.17: страница караоке в две строки капсом с обводкой
        // занимает 0.19 панели, и подпись блока касалась её сверху.
        paddingBottom: panelHeight * 0.2,
      }}
    >
      {block.imagePath && block.kind === 'card' ? (
        <Backdrop src={block.imagePath} />
      ) : null}
      {/* Картинка по теме занимает панель целиком, под надзаголовком. */}
      {block.kind === 'art' && block.imagePath ? (
        <Art
          block={block}
          accentColor={accentColor}
          panelColor={panelColor}
          textColor={textColor}
          panelHeight={panelHeight}
        />
      ) : null}
      {block.kicker ? (
        <div
          style={{
            position: 'absolute',
            top: panelHeight * 0.185,
            fontFamily: FONT,
            fontWeight: 700,
            fontSize: kickerSize,
            letterSpacing: '0.22em',
            // Разрядка добавляет пробел и справа от последней буквы,
            // из-за чего строка стоит левее центра. Возвращаем половину.
            textIndent: '0.22em',
            textTransform: 'uppercase',
            color: accentColor,
            whiteSpace: 'nowrap',
            // Над картинкой надзаголовок терялся на её фоне («ШАГ 4»
            // на коробке) — та же тёмная подложка, что у подписи.
            ...(block.kind === 'art' && block.imagePath
              ? {
                  background: 'rgba(0,0,0,0.62)',
                  padding: `${kickerSize * 0.25}px ${kickerSize * 0.6}px`,
                  borderRadius: kickerSize * 0.4,
                }
              : {}),
          }}
        >
          {block.kicker}
        </div>
      ) : null}

      <div
        style={{
          width: '100%',
          display: 'flex',
          justifyContent: 'center',
          // С надзаголовком содержимое сдвинуто ниже центра: при 0.12
          // плитка знака и окно сайта вставали вплотную под него.
          marginTop: block.kicker ? panelHeight * 0.2 : 0,
          maxHeight: panelHeight * (block.kicker ? 0.6 : 0.84),
          scale: interpolate(enter, [0, 1], [0.965, 1], {
            output: 'perceptual-scale',
          }),
        }}
      >
        {block.kind === 'art' ? null : block.kind === 'media' ? (
          <Screen
            block={block}
            textColor={textColor}
            panelHeight={panelHeight}
            // Ниже отведённого карточкам: запись ещё въезжает снизу и
            // наезжает, и на 0.76 горизонтальный скринкаст упирался в
            // верхний край панели.
            maxHeight={panelHeight * (block.kicker ? 0.5 : 0.62)}
          />
        ) : (
          // Место под текст: от надзаголовка до полосы караоке. Блок
          // стоит по центру с отступом под надзаголовок, поэтому с ним
          // места почти вдвое меньше, чем без него.
          <Fit max={panelHeight * (block.kicker ? 0.43 : 0.68)}>
        {block.kind === 'icon' ? (
          <IconCard
            block={block}
            cardColor={cardColor}
            textColor={textColor}
            panelHeight={panelHeight}
          />
        ) : block.kind === 'counter' ? (
          <Counter
            block={block}
            accentColor={accentColor}
            textColor={textColor}
            panelHeight={panelHeight}
          />
        ) : block.kind === 'scale' ? (
          <Scale
            block={block}
            accentColor={accentColor}
            textColor={textColor}
            panelHeight={panelHeight}
          />
        ) : (
          <Card
            block={block}
            cardColor={cardColor}
            accentColor={accentColor}
            textColor={textColor}
            panelHeight={panelHeight}
            maxHeight={panelHeight * (block.kicker ? 0.6 : 0.84)}
          />
        )}
          </Fit>
        )}
      </div>
    </AbsoluteFill>
  );
};

export const Panel: React.FC<{
  blocks: MotionProps['blocks'];
  panelColor: string;
  glowColor: string;
  cardColor: string;
  accentColor: string;
  textColor: string;
  panelHeight: number;
  // Секунда готового ролика, в которой панель начинается. Блоки живут в
  // абсолютных секундах, а Sequence отсчитывает от своего начала.
  offset: number;
}> = ({
  blocks,
  panelColor,
  glowColor,
  cardColor,
  accentColor,
  textColor,
  panelHeight,
  offset,
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  // Свечение дышит: восемь секунд на цикл. Неподвижное пятно за
  // полминуты превращается в грязь на матрице — глаз перестаёт читать
  // его как свет и начинает как дефект кадра.
  const breath = 0.5 + 0.5 * Math.sin((frame / fps) * ((Math.PI * 2) / 8));

  return (
    <AbsoluteFill style={{background: panelColor, overflow: 'hidden'}}>
      <AbsoluteFill
        style={{
          background: `radial-gradient(circle at 50% 52%, ${glowColor} 0%, rgba(0,0,0,0) 62%)`,
          opacity: interpolate(breath, [0, 1], [0.55, 0.85]),
        }}
      />
      {blocks.map((b, i) => {
        const from = Math.round((b.start - offset) * fps);
        const durationInFrames = Math.round((b.end - b.start) * fps);
        if (durationInFrames <= 0) {
          return null;
        }
        return (
          <Sequence
            key={`${b.start}-${i}`}
            from={from}
            durationInFrames={durationInFrames}
            name={`Панель ${i + 1}`}
          >
            <BlockView
              block={b}
              panelHeight={panelHeight}
              panelColor={panelColor}
              accentColor={accentColor}
              cardColor={cardColor}
              textColor={textColor}
            />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
