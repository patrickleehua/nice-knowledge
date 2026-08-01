"use client";

// 滑块(Base UI Slider):替代原生 <input type="range">,
// 带 aria-valuetext、键盘步进与可见刻度。

import { Slider as SliderPrimitive } from "@base-ui/react/slider";

import { cn } from "@/lib/utils";

function Slider({
  className,
  ticks,
  ...props
}: SliderPrimitive.Root.Props & {
  /** 轨道下方的刻度值,仅作视觉参考 */
  ticks?: number[];
}) {
  return (
    <SliderPrimitive.Root data-slot="slider" {...props}>
      <SliderPrimitive.Control
        className={cn("flex h-5 w-full touch-none items-center", className)}
      >
        <SliderPrimitive.Track className="relative h-1.5 w-full rounded-full bg-muted select-none">
          <SliderPrimitive.Indicator className="absolute h-full rounded-full bg-primary select-none" />
          <SliderPrimitive.Thumb className="size-4 rounded-full bg-background ring-2 ring-primary transition-shadow select-none focus-visible:ring-4 focus-visible:outline-none" />
          {ticks?.map((tick) => (
            <span
              key={tick}
              aria-hidden
              className="absolute top-2.5 h-1 w-px bg-border"
              style={{
                left: `${((tick - Number(props.min ?? 0)) / (Number(props.max ?? 100) - Number(props.min ?? 0))) * 100}%`,
              }}
            />
          ))}
        </SliderPrimitive.Track>
      </SliderPrimitive.Control>
    </SliderPrimitive.Root>
  );
}

export { Slider };
