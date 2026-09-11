import React from 'react';
import {
  AbsoluteFill,
  CanvasImage,
  Easing,
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
  const size = panelHeight * 0.38;

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
            borderRadius: size * 0.24,
            background: cardColor,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            overflow: 'hidden',
            scale: interpolate(pop, [0, 1], [0.7, 1], {
              output: 'perceptual-scale',
            }),
          }}
        >
          <CanvasImage
            src={staticFile(block.imagePath)}
            style={{width: '78%', height: 'auto', objectFit: 'contain'}}
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
      <div
        style={{
          width: '82%',
          maxHeight,
          borderRadius: radius,
          overflow: 'hidden',
          background: cardColor,
          display: 'flex',
          flexDirection: 'column',
        }}
      >
        <CanvasImage
          src={staticFile(block.imagePath)}
          style={{
            width: '100%',
            height: 'auto',
            minHeight: 0,
            objectFit: 'contain',
            display: 'block',
          }}
        />
        {block.items.length > 0 ? (
          <div style={{padding: `${panelHeight * 0.05}px ${panelHeight * 0.06}px`}}>
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

const BlockView: React.FC<{
  block: MotionBlock;
  panelHeight: number;
  accentColor: string;
  cardColor: string;
  textColor: string;
}> = ({block, panelHeight, accentColor, cardColor, textColor}) => {
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
        paddingBottom: panelHeight * 0.17,
      }}
    >
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
          marginTop: block.kicker ? panelHeight * 0.12 : 0,
          maxHeight: panelHeight * (block.kicker ? 0.6 : 0.84),
          scale: interpolate(enter, [0, 1], [0.965, 1], {
            output: 'perceptual-scale',
          }),
        }}
      >
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
