import React from 'react';
import {Composition} from 'remotion';
import {ReelTemplate} from './ReelTemplate';
import {reelPropsSchema, type ReelProps} from './props';

export const Root: React.FC = () => {
  return (
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
  );
};
