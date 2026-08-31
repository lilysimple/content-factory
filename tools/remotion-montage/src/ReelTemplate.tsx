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
import {Video} from '@remotion/media';
import {Captions} from './Captions';
import {Cover} from './Cover';
import type {ReelProps} from './props';

const FONT = 'system-ui, -apple-system, Helvetica, sans-serif';

// ── куда смотрит кадр ────────────────────────────────────────────────
//
// Дубль почти никогда не 9:16, и кроп решает, что человек увидит. Центр
// холста — плохой ответ для записи экрана: работа идёт под курсором, а
// он гуляет по всей ширине. Трек «куда смотреть» считает питон разницей
// соседних кадров; здесь мы только выбираем видимое окно и сглаживаем
// путь между точками.
//
// Пропы `crop*` из remotion-markup/cropping.md здесь не подходят, и это
// проверено рендером: они вырезают часть коробки элемента и оставляют
// вокруг пустоту, а нам нужно обратное — вырезанное окно должно занять
// весь холст. Поэтому коробка растягивается до размера «кадр целиком», а
// нужное окно наезжает на холст сдвигом. Кроп при этом честный: видео не
// растянуто, пропорции сохранены.

type Focus = {t: number; x: number; y: number};

function focusAt(track: Focus[], t: number): {x: number; y: number} {
  if (track.length === 0) return {x: 0.5, y: 0.5};
  if (t <= track[0].t) return track[0];
  const last = track[track.length - 1];
  if (t >= last.t) return last;
  let lo = 0;
  while (lo < track.length - 2 && track[lo + 1].t < t) lo++;
  const a = track[lo];
  const b = track[lo + 1];
  const k = b.t === a.t ? 0 : (t - a.t) / (b.t - a.t);
  return {x: a.x + (b.x - a.x) * k, y: a.y + (b.y - a.y) * k};
}

function clamp(v: number, min: number, max: number) {
  return Math.min(max, Math.max(min, v));
}

const Clip: React.FC<{
  src: string;
  from: number;
  to: number;
  offset: number;    // секунда готового ролика, с которой идёт этот кусок
  track: Focus[];
  videoWidth: number;
  videoHeight: number;
}> = ({src, from, to, offset, track, videoWidth, videoHeight}) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();

  // Во сколько раз растянуть исходный кадр, чтобы он закрыл холст по
  // обеим сторонам, и насколько он при этом вылезет за края.
  const scale = Math.max(width / videoWidth, height / videoHeight);
  const boxW = videoWidth * scale;
  const boxH = videoHeight * scale;

  const {x, y} = focusAt(track, offset + frame / fps);
  const left = clamp(width / 2 - x * boxW, width - boxW, 0);
  const top = clamp(height / 2 - y * boxH, height - boxH, 0);

  return (
    <AbsoluteFill style={{overflow: 'hidden'}}>
      <Video
        src={src}
        trimBefore={Math.round(from * fps)}
        trimAfter={Math.round(to * fps)}
        // Коробка посчитана точно по пропорции дубля, так что вписывать
        // нечего — но `cover` страхует от округления в полпикселя.
        objectFit="cover"
        style={{
          position: 'absolute',
          left,
          top,
          width: boxW,
          height: boxH,
        }}
      />
    </AbsoluteFill>
  );
};

// ── титул хука ───────────────────────────────────────────────────────
//
// Подпорка для дубля без звука: караоке там взять неоткуда, а мысль на
// экране держать всё равно надо.

const HookCaption: React.FC<{text: string}> = ({text}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  return (
    <AbsoluteFill style={{justifyContent: 'flex-end', alignItems: 'center'}}>
      <div
        style={{
          opacity: interpolate(frame, [0, fps * 0.4], [0, 1], {
            extrapolateLeft: 'clamp',
            extrapolateRight: 'clamp',
            easing: Easing.bezier(0.16, 1, 0.3, 1),
          }),
          margin: '0 6% 9%',
          padding: '20px 28px',
          borderRadius: 20,
          background: 'rgba(0,0,0,0.55)',
          color: '#fff',
          fontFamily: FONT,
          fontWeight: 700,
          fontSize: 46,
          lineHeight: 1.25,
          textAlign: 'center',
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  );
};

// ── первый кадр и последний ──────────────────────────────────────────

const OutroCard: React.FC<{
  text: string;
  color: string;
  brandName?: string;
}> = ({text, color, brandName}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  return (
    <AbsoluteFill
      style={{
        background: color,
        justifyContent: 'center',
        alignItems: 'center',
        padding: '10%',
      }}
    >
      <div
        style={{
          opacity: interpolate(frame, [0, fps * 0.25], [0, 1], {
            extrapolateLeft: 'clamp',
            extrapolateRight: 'clamp',
            easing: Easing.bezier(0.16, 1, 0.3, 1),
          }),
          scale: interpolate(frame, [0, fps * 0.35], [0.92, 1], {
            extrapolateLeft: 'clamp',
            extrapolateRight: 'clamp',
            easing: Easing.bezier(0.16, 1, 0.3, 1),
            output: 'perceptual-scale',
          }),
          color: '#fff',
          fontFamily: FONT,
          fontWeight: 800,
          fontSize: 58,
          lineHeight: 1.3,
          textAlign: 'center',
        }}
      >
        {text}
        {brandName ? (
          <div style={{marginTop: 28, fontSize: 30, fontWeight: 500, opacity: 0.85}}>
            {brandName}
          </div>
        ) : null}
      </div>
    </AbsoluteFill>
  );
};

// ── сборка ───────────────────────────────────────────────────────────

export const ReelTemplate: React.FC<ReelProps> = (props) => {
  const {fps} = useVideoConfig();
  const introFrames = Math.round(props.introSeconds * fps);
  const outroFrames = Math.round(props.outroSeconds * fps);

  // Куски идут встык: вырезанная пауза — это склейка, и выглядеть она
  // должна склейкой, а не затемнением. Смягчать её переходом значит
  // возвращать в ролик те же полсекунды, ради которых её и вырезали.
  //
  // Клипы собираются из данных, а не расписаны руками, как советует
  // remotion-markup/video-editing.md: тот совет про монтаж человеком в
  // Studio, а здесь дорожку целиком пересобирает следующий прогон, и
  // рукописные клипы устарели бы на первом же новом дубле.
  let clock = 0;
  const pieces = props.segments.map((s) => {
    const offset = clock;
    const seconds = Math.max(0, s.to - s.from);
    clock += seconds;
    return {...s, offset, frames: Math.max(1, Math.round(seconds * fps))};
  });
  const bodyFrames = pieces.reduce((n, p) => n + p.frames, 0);

  const hasIntro = Boolean(
    props.coverPath || props.title || props.coverLines.length > 0,
  );

  return (
    <AbsoluteFill style={{backgroundColor: '#000'}}>
      {hasIntro ? (
        <Sequence from={0} durationInFrames={introFrames} name="Первый кадр">
          {props.coverPath ? (
            <>
              <CanvasImage
                src={staticFile(props.coverPath)}
                style={{
                  width: '100%',
                  height: '100%',
                  objectFit: 'cover',
                  ...(props.coverBlur
                    ? {filter: 'blur(16px)', scale: 1.12}
                    : {}),
                }}
              />
              {/* Затемнение: под заголовком лежит либо обложка со своей
                  вёрсткой, либо кадр из дубля, и белый текст на нём
                  читается через раз. */}
              <AbsoluteFill
                style={{
                  // Сила затемнения приходит из ТЗ обложки: на кадре с
                  // человеком его почти не нужно, на записи экрана без
                  // него не читается ничего.
                  background: `linear-gradient(180deg, rgba(0,0,0,${(
                    props.scrim * 0.5
                  ).toFixed(2)}) 0%, rgba(0,0,0,${(props.scrim * 1.4).toFixed(
                    2,
                  )}) 55%, rgba(0,0,0,${Math.min(0.85, props.scrim * 2).toFixed(
                    2,
                  )}) 100%)`,
                }}
              />
            </>
          ) : (
            <AbsoluteFill style={{background: props.brandColor}} />
          )}
          <Cover
            lines={props.coverLines}
            title={props.title}
            titleColor={props.titleColor}
            brandName={props.brandName}
            font={props.coverFont}
            weight={props.coverWeight}
            titleSize={props.titleSize}
          />
        </Sequence>
      ) : null}

      {pieces.map((p, i) => (
        <Sequence
          key={`${p.from}-${i}`}
          from={introFrames + Math.round(p.offset * fps)}
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
          />
        </Sequence>
      ))}

      <Sequence from={introFrames} durationInFrames={bodyFrames} name="Слова">
        {props.pages.length > 0 ? (
          <Captions pages={props.pages} accent={props.accentColor} />
        ) : props.hook ? (
          <Sequence from={0} durationInFrames={Math.min(bodyFrames, 3 * fps)}>
            <HookCaption text={props.hook} />
          </Sequence>
        ) : null}
      </Sequence>

      {props.cta ? (
        <Sequence
          from={introFrames + bodyFrames}
          durationInFrames={outroFrames}
          name="Последний кадр"
        >
          <OutroCard
            text={props.cta}
            color={props.brandColor}
            brandName={props.brandName}
          />
        </Sequence>
      ) : null}
    </AbsoluteFill>
  );
};
