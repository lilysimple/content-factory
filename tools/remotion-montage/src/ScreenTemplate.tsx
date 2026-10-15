import React from 'react';
import {AbsoluteFill, staticFile} from 'remotion';
import {Video} from '@remotion/media';
import {Captions} from './Captions';
import type {ScreenProps} from './screenProps';

// Запись экрана на вертикальном холсте.
//
// Кроп, которым ролик сажает дубль 16:9 на 9:16, здесь вреден: на дубле
// за краем остаётся стена, а на записи экрана — половина работы. Эта
// запись шириной 1406 потеряла бы около шестисот точек, то есть ровно ту
// панель, ради которой её и снимали.
//
// Поэтому картинка вписывается целиком, а поля закрывает её же
// размытая копия: чёрные полосы в ленте читаются как брак загрузки, а
// размытый фон — как кадр. Копия идёт тем же файлом и тем же временем,
// лишнего декодирования тут нет, Remotion держит один источник.

export const ScreenTemplate: React.FC<ScreenProps> = ({
  videoPath,
  videoWidth,
  videoHeight,
  pages,
  accentColor,
  captionTextColor,
  width,
  height,
}) => {
  const src = staticFile(videoPath);

  // Вписанная картинка: ширина холста целиком, высота по пропорции.
  const fitH = Math.round((width * videoHeight) / videoWidth);
  // Поле под субтитр отмеряется от низа картинки, а не от низа холста:
  // на записи другой пропорции строка иначе уехала бы на саму картинку.
  const top = Math.round((height - fitH) / 2);
  const captionTop = Math.min(top + fitH + 56, height - 320);

  return (
    <AbsoluteFill style={{backgroundColor: '#0E0E10'}}>
      {/* Фон: та же запись, растянутая по накрывающей стороне и уведённая
          в размытие. Масштаб чуть больше накрывающего — размытие съедает
          края, и без запаса по периметру остаётся светлая кайма. */}
      <AbsoluteFill style={{overflow: 'hidden'}}>
        <Video
          src={src}
          muted
          style={{
            position: 'absolute',
            width: '120%',
            height: '120%',
            left: '-10%',
            top: '-10%',
            objectFit: 'cover',
            filter: 'blur(48px) saturate(1.1) brightness(0.55)',
          }}
        />
      </AbsoluteFill>

      {/* Сама запись. Звук берётся отсюда: дорожку озвучки свёл в этот же
          файл скилл озвучки, и второго источника звука у композиции нет. */}
      <Video
        src={src}
        style={{
          position: 'absolute',
          left: 0,
          top,
          width,
          height: fitH,
          // Скруглением и тенью запись отделяется от собственного фона:
          // без них вписанный кадр читается как подтёкшая картинка, а не
          // как окно поверх неё.
          borderRadius: 24,
          boxShadow: '0 24px 80px rgba(0,0,0,0.55)',
        }}
      />

      <Captions
        pages={pages}
        accent={accentColor}
        textColor={captionTextColor}
        look={{top: captionTop, fontSize: 58, plate: true, shadow: 'dark'}}
      />
    </AbsoluteFill>
  );
};
