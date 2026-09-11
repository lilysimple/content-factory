import React from 'react';
import {AbsoluteFill, Sequence, staticFile, useVideoConfig} from 'remotion';
import {Captions} from './Captions';
import {Clip} from './Clip';
import {Panel} from './Panel';
import {OutroCard} from './ReelTemplate';
import type {MotionProps} from './motionProps';

// Сборка сплита.
//
// Холст поделён швом на две половины: дубль и панель. Шов жёсткий, без
// растушёвки, и это решение, а не упрощение — в разобранном примере он
// тоже жёсткий. Мягкий переход между записью экрана и лицом читается как
// брак склейки: глаз ищет, где кончается одна картинка, и не находит.
//
// Субтитр садится ровно на шов и живёт плашкой. Градиент, которым он
// отбит в `Reel`, здесь затемнял бы низ панели — то есть портил бы
// картинку, ради которой панель и заведена.

export const MotionTemplate: React.FC<MotionProps> = (props) => {
  const {fps, width, height} = useVideoConfig();

  const panelHeight = Math.round(height * props.split);
  const videoHeight = height - panelHeight;
  const panelOnTop = props.headAnchor === 'bottom';
  const seam = panelOnTop ? panelHeight : videoHeight;

  // Куски идут встык: вырезанная пауза — это склейка, и выглядеть она
  // должна склейкой. Считаем то же, что и `Reel`, но кроп у клипа уже
  // не по холсту, а по своей половине.
  let clock = 0;
  const pieces = props.segments.map((s) => {
    const offset = clock;
    const seconds = Math.max(0, s.to - s.from);
    clock += seconds;
    return {...s, offset, frames: Math.max(1, Math.round(seconds * fps))};
  });
  const bodyFrames = pieces.reduce((n, p) => n + p.frames, 0);

  const splitBlocks = props.blocks.filter((b) => !b.full);
  const fullBlocks = props.blocks.filter((b) => b.full);

  return (
    <AbsoluteFill style={{backgroundColor: props.panelColor}}>
      {/* ── половина с дублем ── */}
      <div
        style={{
          position: 'absolute',
          left: 0,
          top: panelOnTop ? panelHeight : 0,
          width,
          height: videoHeight,
          overflow: 'hidden',
        }}
      >
        {pieces.map((p, i) => (
          <Sequence
            key={`${p.from}-${i}`}
            from={Math.round(p.offset * fps)}
            durationInFrames={p.frames}
            name={`Кусок ${i + 1}`}
          >
            <Clip
              src={staticFile(props.videoPath)}
              from={p.from}
              to={p.to}
              offset={p.offset}
              track={props.pan}
              videoWidth={props.videoWidth}
              videoHeight={props.videoHeight}
              boxWidth={width}
              boxHeight={videoHeight}
            />
          </Sequence>
        ))}
      </div>

      {/* ── половина с панелью ── */}
      <div
        style={{
          position: 'absolute',
          left: 0,
          top: panelOnTop ? 0 : videoHeight,
          width,
          height: panelHeight,
          overflow: 'hidden',
        }}
      >
        <Panel
          blocks={splitBlocks}
          panelColor={props.panelColor}
          glowColor={props.glowColor}
          cardColor={props.cardColor}
          accentColor={props.accentColor}
          textColor={props.textColor}
          panelHeight={panelHeight}
          offset={0}
        />
      </div>

      {/* ── блоки на весь кадр ──
          Тот же компонент поверх обеих половин: фон закрывает холст
          целиком, а карточка остаётся на своём месте и своего размера.

          Больше она не становится, и это правка по двум рендерам:
          кегли считаются от высоты панели, на полном кадре высота
          вчетверо больше — «Модель сама решает, насколько глубоко
          думать» уезжала за нижний край карточки обрезанной. Заодно
          пропал прыжок: блок на весь кадр отличается от соседнего тем,
          что дубль ушёл, а не тем, что вёрстка другая. */}
      {fullBlocks.map((b, i) => {
        const from = Math.round(b.start * fps);
        const durationInFrames = Math.round((b.end - b.start) * fps);
        if (durationInFrames <= 0) {
          return null;
        }
        return (
          <Sequence
            key={`full-${b.start}-${i}`}
            from={from}
            durationInFrames={durationInFrames}
            name={`Панель целиком ${i + 1}`}
          >
            <AbsoluteFill style={{backgroundColor: props.panelColor}}>
              <div
                style={{
                  position: 'absolute',
                  left: 0,
                  top: panelOnTop ? 0 : videoHeight,
                  width,
                  height: panelHeight,
                }}
              >
                <Panel
                  blocks={[
                    {
                      ...b,
                      start: 0,
                      end: b.end - b.start,
                      // Пункты списка приходят в секундах ролика, а блок
                      // тут начинается заново с нуля — сдвигаем вместе с
                      // ним, иначе список на полном кадре не выедет.
                      items: b.items.map((it) => ({
                        ...it,
                        at: it.at - b.start,
                      })),
                      valueAt:
                        b.valueAt === undefined
                          ? undefined
                          : b.valueAt - b.start,
                    },
                  ]}
                  panelColor={props.panelColor}
                  glowColor={props.glowColor}
                  cardColor={props.cardColor}
                  accentColor={props.accentColor}
                  textColor={props.textColor}
                  panelHeight={panelHeight}
                  offset={0}
                />
              </div>
            </AbsoluteFill>
          </Sequence>
        );
      })}

      {/* ── слова ──
          Поверх всего, включая блоки на весь кадр: речь идёт непрерывно,
          и пропадающий на полном кадре субтитр читается как сбой. Шов
          держит строку и там: карточка на полном кадре стоит на своём
          месте, и под ней ровно то же пустое поле.

          Строка висит **над** швом. Под ним начинается дубль, а в
          вертикальном кадре лицо стоит ровно под швом — слова ложились
          человеку на лицо. */}
      <Sequence from={0} durationInFrames={bodyFrames} name="Слова">
        {props.pages.length > 0 ? (
          <Captions
            pages={props.pages}
            accent={props.accentColor}
            look={{
              top: seam,
              above: true,
              fontSize: Math.round(height * 0.032),
              uppercase: true,
              plate: true,
            }}
          />
        ) : null}
      </Sequence>

      {props.cta ? (
        <Sequence
          from={bodyFrames}
          durationInFrames={Math.round(props.outroSeconds * fps)}
          name="Последний кадр"
        >
          <OutroCard
            text={props.cta}
            color={props.panelColor}
            brandName={props.brandName}
          />
        </Sequence>
      ) : null}
    </AbsoluteFill>
  );
};
