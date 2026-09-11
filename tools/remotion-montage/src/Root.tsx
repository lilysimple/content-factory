import React from 'react';
import {Composition} from 'remotion';
import {MotionTemplate} from './MotionTemplate';
import {ReelTemplate} from './ReelTemplate';
import {motionPropsSchema, type MotionProps} from './motionProps';
import {reelPropsSchema, type ReelProps} from './props';

export const Root: React.FC = () => {
  return (
    <>
    <Composition
      id="Reel"
      component={ReelTemplate}
      schema={reelPropsSchema}
      // Значения ниже перекрываются --props в реальном рендере: питон
      // всегда передаёт разобранный дубль, иначе длительность и картинка
      // разъедутся.
      durationInFrames={30 * 30}
      fps={30}
      width={1080}
      height={1920}
      defaultProps={{
        videoPath: '',
        coverBlur: false,
        coverFocus: {x: 0.5, y: 0.5},
        coverAnchor: 'bottom',
        coverInset: 0,
        coverLines: [],
        coverFont: 'Manrope',
        coverWeight: 800,
        titleColor: '#FFFFFF',
        titleSize: 38,
        scrim: 0.28,
        videoWidth: 1920,
        videoHeight: 1080,
        segments: [{from: 0, to: 25}],
        pan: [],
        pages: [],
        brandColor: '#111111',
        accentColor: '#C97C5D',
        width: 1080,
        height: 1920,
        fps: 30,
        introSeconds: 1.4,
        outroSeconds: 1.8,
      } satisfies ReelProps}
      // Длина ролика считается от того, что осталось после нарезки пауз,
      // а не от длины присланного файла. Пока здесь стояла длительность
      // дубля, вырезанные секунды доезжали до конца ролика чёрным хвостом.
      calculateMetadata={async ({props}) => {
        const fps = props.fps ?? 30;
        const body = (props.segments ?? []).reduce(
          (n, s) => n + Math.max(0, s.to - s.from),
          0,
        );
        const total =
          (props.introSeconds ?? 1.4) + body + (props.outroSeconds ?? 1.8);
        return {
          durationInFrames: Math.max(1, Math.round(total * fps)),
          fps,
          width: props.width ?? 1080,
          height: props.height ?? 1920,
        };
      }}
    />

    {/* Сплит: инфографика и говорящая голова в одном кадре. Отдельная
        композиция, а не флаг у `Reel`, потому что вход другой — панель
        приходит своими блоками, и держать их в схеме ролика, где их
        никогда не бывает, значит платить за них на каждом монтаже. */}
    <Composition
      id="Motion"
      component={MotionTemplate}
      schema={motionPropsSchema}
      durationInFrames={30 * 30}
      fps={30}
      width={1080}
      height={1920}
      defaultProps={{
        videoPath: '',
        videoWidth: 1920,
        videoHeight: 1080,
        segments: [{from: 0, to: 25}],
        pan: [],
        headAnchor: 'bottom',
        split: 0.448,
        panelColor: '#020203',
        glowColor: '#1B2A6B',
        cardColor: '#1E1F29',
        accentColor: '#C3865F',
        blocks: [],
        pages: [],
        width: 1080,
        height: 1920,
        fps: 30,
        outroSeconds: 1.8,
      } satisfies MotionProps}
      calculateMetadata={async ({props}) => {
        const fps = props.fps ?? 30;
        const body = (props.segments ?? []).reduce(
          (n, s) => n + Math.max(0, s.to - s.from),
          0,
        );
        const total = body + (props.cta ? props.outroSeconds ?? 1.8 : 0);
        return {
          durationInFrames: Math.max(1, Math.round(total * fps)),
          fps,
          width: props.width ?? 1080,
          height: props.height ?? 1920,
        };
      }}
    />
    </>
  );
};
