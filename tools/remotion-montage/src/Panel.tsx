import React from 'react';
import {
  AbsoluteFill,
  CanvasImage,
  Easing,
  Sequence,
  interpolate,
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

const Card: React.FC<{
  block: MotionBlock;
  cardColor: string;
  panelHeight: number;
}> = ({block, cardColor, panelHeight}) => {
  if (!block.imagePath && block.lines.length === 0) {
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
          borderRadius: radius,
          overflow: 'hidden',
          background: cardColor,
          display: 'flex',
        }}
      >
        <CanvasImage
          src={staticFile(block.imagePath)}
          style={{width: '100%', height: 'auto', display: 'block'}}
        />
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
        color: '#fff',
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
    </div>
  );
};

const BlockView: React.FC<{
  block: MotionBlock;
  panelHeight: number;
  accentColor: string;
  cardColor: string;
}> = ({block, panelHeight, accentColor, cardColor}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

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
      }}
    >
      {block.kicker ? (
        <div
          style={{
            position: 'absolute',
            top: panelHeight * 0.185,
            fontFamily: FONT,
            fontWeight: 700,
            fontSize: panelHeight * 0.062,
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
          scale: interpolate(enter, [0, 1], [0.965, 1], {
            output: 'perceptual-scale',
          }),
        }}
      >
        <Card
          block={block}
          cardColor={cardColor}
          panelHeight={panelHeight}
        />
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
            />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
