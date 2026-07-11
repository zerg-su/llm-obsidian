---
type: concept
title: "SVG Diagram Style Guide"
created: 2026-04-14
updated: 2026-07-11
tags:
  - design
  - svg
  - brand
  - diagrams
status: evergreen
related:
  - "[[index]]"
sources: []
sessions:
  - public-template-v2
---

# SVG Diagram Style Guide

Каноничный визуальный стиль для всех архитектурных и концептуальных диаграмм в этом плагине. Извлечён из 17 production SVG-диаграмм. Используйте как reference при создании или обновлении SVG.

## Шрифт

```
font-family: 'Space Grotesk', system-ui, -apple-system, sans-serif
```

Space Grotesk — единственный typeface. Никаких fallback на serif или monospace.

## Палитра цветов

### Core (использовать в каждой диаграмме)

| Token | Hex | Роль |
|---|---|---|
| bg | #0A0A0A | Canvas-фон (near-black) |
| card | #111111 | Card/container fill |
| card-inner | #1A1A1A | Nested-элемент fill |
| border | #2D2D2D | Card-borders, разделители |
| text-primary | #F5F5F0 | Заголовки, лейблы (off-white) |
| text-secondary | #888888 | Описания, captions |
| text-tertiary | #6a6a6a | De-emphasized метаданные |
| accent | #E07850 | Primary accent, стрелки, highlights (warm rust-orange) |
| accent-bright | #FF6B35 | Secondary accent, hover states (brighter orange) |

### Platform/Category цвета (для разнообразия в диаграмме)

| Token | Hex | Типичное использование |
|---|---|---|
| blue | #60A5FA | Google, data, информация |
| purple | #8b5cf6 | Meta, стратегия, креатив |
| cyan | #06b6d4 | LinkedIn, networking |
| green | #4ADE80 | Success, validation, TikTok |
| rose | #F43F5E | YouTube, alerts |
| orange | #FF6B35 | Microsoft, secondary accent |
| gray | #888888 | Нейтральный, generic platforms |

### Status-цвета (pass/warn/fail индикаторы)

| Token | Hex | Роль |
|---|---|---|
| pass | #16a34a | Pass, success |
| warn | #f59e0b | Warning, attention |
| fail | #dc2626 | Fail, critical |

## Typography Scale

| Element | Size | Weight | Color | Extra |
|---|---|---|---|---|
| Diagram title | 16-17px | 700 | #F5F5F0 | text-anchor: middle |
| Subtitle | 11px | 400 | #888888 | text-anchor: middle |
| Section label | 13px | 700 | accent color | letter-spacing: 2 |
| Card heading | 12-15px | 600-700 | #F5F5F0 | text-anchor: middle |
| Card subtext | 9-11px | 400 | accent color | Skill/agent name |
| Body text | 10px | 400 | #888888 | Описания |
| Tiny label | 9px | 400 | #6a6a6a | Метаданные, counts |

## Layout Primitives

### Outer Container
```xml
<rect width="800" height="500" fill="#0A0A0A"/>
```
Стандартный canvas 800x500. Некоторые диаграммы 900x250 или 900x350 в зависимости от content'а.

### Card
```xml
<rect x="40" y="20" width="720" height="120" rx="16" fill="#111111" stroke="#2D2D2D" stroke-width="1.5"/>
```
- Corner radius: `rx="16"` для outer containers
- Border: `#2D2D2D`, `stroke-width="1.5"`

### Colored Top Bar (card accent)
```xml
<rect x="40" y="20" width="720" height="4" rx="2" fill="#E07850"/>
```
4px height, на верхней грани card. Цвет индицирует category.

### Inner Card (nested element)
```xml
<rect x="60" y="230" width="105" height="60" rx="6" fill="#1A1A1A" stroke="#2D2D2D" stroke-width="1"/>
```
- Corner radius: `rx="6"` для small inner cards, `rx="9"` для medium
- Fill: `#1A1A1A` (slightly lighter than parent card)

### Numbered Circle (для sequences)
```xml
<circle cx="138" cy="60" r="14" fill="#0A0A0A" stroke="#60A5FA" stroke-width="1.5"/>
<text x="138" y="60" font-size="12" fill="#60A5FA" text-anchor="middle" font-weight="bold" dominant-baseline="central">1</text>
```
Circle stroke-color матчит step's category-color.

### Arrow Connector
```xml
<line x1="400" y1="140" x2="400" y2="170" stroke="#E07850" stroke-width="1.5"/>
<polygon points="394,167 400,177 406,167" fill="#E07850"/>
```
Всегда `#E07850`. Vertical для flow-down, horizontal для left-to-right pipelines.

### Horizontal Divider (title underline)
```xml
<line x1="380" y1="36" x2="520" y2="36" stroke="#E07850" stroke-width="2.5" stroke-linecap="round"/>
```
Короткая центрированная линия под title. Всегда accent-color.

## Правила

1. Всегда dark-theme. Никаких white или light-фонов.
2. Только Space Grotesk. Никаких других шрифтов.
3. `#E07850` — signature accent. Использовать для arrows, highlights, primary visual element.
4. Cards всегда имеют `#2D2D2D` borders. Никаких borderless cards.
5. Colored top bars (4px) идентифицируют categories. Один цвет на category, консистентно через диаграмму.
6. Текст всегда left-aligned или center-aligned. Никогда right-aligned.
7. Никаких gradients, shadows, blur-фильтров. Только flat-design.
8. Numbered circles для sequential-шагов. Цвет матчит category.
9. Arrow connectors всегда `#E07850` с triangle-tips.
10. Naming файлов: zero-padded number prefix (01-, 02-, etc.) + kebab-case description.
