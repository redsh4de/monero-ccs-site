import type { ImageMetadata } from "astro";

type Mod = { default: ImageMetadata };

const maskIcons: Record<string, ImageMetadata> = {};
for (const [path, mod] of Object.entries(
  import.meta.glob<Mod>("@/assets/icons/mask/**/*.avif", { eager: true }),
)) {
  const i = path.lastIndexOf("mask/");
  const key = path.slice(i + "mask/".length).replace(/\.avif$/, "");
  maskIcons[key] = mod.default;
}

export const icons = {
  mask(name: string): ImageMetadata {
    const m = maskIcons[name];
    if (!m) throw new Error(`Unknown mask icon: "${name}"`);
    return m;
  },
};
